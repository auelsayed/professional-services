#!/usr/bin/env python3

# Copyright 2022 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import logging
import time
import requests
import re
import firebase_admin
from datetime import datetime
from googleapiclient.discovery import build_from_document, build
from firestore_write import firestore_write
from get_firestore_cases import get_firestore_cases
from get_firestore_first_in import get_firestore_first_in
from firestore_delete_cases import firestore_delete_cases
from notify_slack import notify_slack
from support_case import SupportCase
from firebase_admin import credentials
from firebase_admin import firestore
from get_parent import get_parent
from support_subscribe_email import support_subscribe_email
from support_add_comment import support_add_comment

logger = logging.getLogger(__name__)
MAX_RETRIES = 3


def case_updates(is_test):
  """
    Infinite loop that pulls all of the open Google Cloud support cases for our
    org and their associated public comments every 15 seconds and compares it
    to the cases and comments from the previous pull. If any change is detected
    between the two versions of the case, the change is posted to any channel
    that is tracking it.

    Parameters
    ----------
    is_test : bool
      flag indicating if we are running the loop a single time for testing
    """
  ORG_ID = os.environ.get("ORG_ID")
  API_KEY = os.environ.get("API_KEY")
  query_string = f"organization='organizations/{ORG_ID}' AND state=OPEN"

  # Get our discovery doc and build our service
  r = requests.get(
      f"https://cloudsupport.googleapis.com/$discovery/rest?key={API_KEY}&labels=V2_TRUSTED_TESTER&version=v2beta",
      timeout=5)
  r.raise_for_status()
  support_service = build_from_document(r.json())

  if not firebase_admin._apps:
    PROJECT_ID = os.environ.get("PROJECT_ID")
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {
        "projectId": PROJECT_ID,
    })

  while True:
    loop_skip = False
    sleep_timer = 10
    closed_cases = []
    cases = get_firestore_cases()
    req = support_service.cases().search(query=query_string)
    try:
      resp = req.execute(num_retries=MAX_RETRIES).get("cases", [])
    except BrokenPipeError as e:
      error_message = f"{e} : {datetime.now()}"
      logger.error(error_message)
      time.sleep(5)
      continue

    temp_cases = []

    for case in resp:
      try:
        temp_case = SupportCase(case)
      except NameError as e:
        error_message = f"{e} : {datetime.now()}"
        logger.error(error_message)
        loop_skip = True
        break
      else:
        temp_cases.append(vars(temp_case))

    if loop_skip:
      time.sleep(5)
      continue

    # Check for cases that have closed since the last loop and notify slack
    for fs_case in cases:
      delete_entry = True
      if fs_case["update_time"] == "2100-12-31 23:59:59+00:00":
        delete_entry = False
      else:
        for t_case in temp_cases:
          if t_case["case_number"] == fs_case["case_number"]:
            delete_entry = False
            break
      if delete_entry:
        fs_case["update_time"] = "2100-12-31 23:59:59+00:00"
        guid = firestore_write("cases", fs_case)
        first_doc_in = get_firestore_first_in(fs_case["case_number"],
                                              fs_case["update_time"])
        if first_doc_in:
          if guid == first_doc_in["guid"]:
            notify_slack(fs_case["case_number"], "closed", "")
            closed_cases.append(fs_case["case_number"])

    # Check for existing cases that have a new update time. Post their relevant
    # update to the channels that are tracking those cases.
    for t_case in temp_cases:
      is_new = True
      for fs_case in cases:
        if t_case["case_number"] == fs_case["case_number"]:
          is_new = False
          if not t_case["update_time"] == fs_case["update_time"]:
            guid = firestore_write("cases", t_case)
            first_doc_in = get_firestore_first_in(t_case["case_number"],
                                                  t_case["update_time"])
          if fs_case["comment_list"] != t_case["comment_list"]:
            if "googleSupport" in t_case["comment_list"][0]["creator"]:
              if guid == first_doc_in["guid"]:
                notify_slack(t_case["case_number"], "comment",
                             t_case["comment_list"][0]["body"])
          if fs_case["priority"] != t_case["priority"]:
            if guid == first_doc_in["guid"]:
              notify_slack(t_case["case_number"], "priority",
                           t_case["priority"])
          if fs_case["escalated"] != t_case["escalated"]:
            if t_case["escalated"]:
              if guid == first_doc_in["guid"]:
                notify_slack(t_case, "escalated", t_case["escalated"])
            else:
              if guid == first_doc_in["guid"]:
                notify_slack(t_case["case_number"], "de-escalated",
                             t_case["escalated"])

      if is_new:
        firestore_write("cases", t_case)
        auto_cc(t_case)

    # Wait to try again so we don"t spam the API
    time.sleep(sleep_timer)

    # Delete closed cases after waiting to minimize duplicate Slack updates
    for case in closed_cases:
      firestore_delete_cases(case)
    if is_test:
      break


def auto_cc(case):
  # Loop through all the Channel IDs and check which ones have the new case in
  # their auto cc tracking
  collection = "tracked_assets"
  db = firestore.client()
  tracked_assets = db.collection(collection).get()
  case_num = case["case_number"]
  case_parent = get_parent(case_num)
  project_id = re.search("projects\/[^\/]+", case_parent).group()

  with build("cloudresourcemanager", "v3") as service:
    projects = service.projects()
    try:
      req = projects.get(name=project_id)
      case_project = req.execute(num_retries=MAX_RETRIES)

    except BrokenPipeError as e:
      error_message = f"{e} : {datetime.now()}"
      logger.error(error_message)

      return

    project_parent = case_project["parent"]
    project_id = project_id.split("/")[1]
    org_id = re.search("organizations\/\d+", project_parent)
    org_id = org_id.group().split("/")[1] if org_id else org_id
    folders = re.findall("folders\/[^\/]+", project_parent)

  for channel in tracked_assets:
    channel_doc = db.document(f"{collection}/{channel.id}")

    # Organization check
    org_new_emails = tracking_check(channel_doc, "organizations", org_id,
                                    case_num)
    # Folder check
    folder_new_emails = []
    for folder in folders:
      folder_new_emails.extend(
          tracking_check(channel_doc, "folders", folder, case_num)[0])
    # Project check
    project_new_emails = tracking_check(channel_doc, "projects", project_id,
                                        case_num)

    combined_new_emails = [
        ", ".join(org_new_emails), ", ".join(folder_new_emails),
        ", ".join(project_new_emails)
    ]

    if combined_new_emails:
      response = ("The following emails have been added automatically through"
                  f" asset subscription: {', '.join(combined_new_emails)}")

      # Write a comment on the case to notify all newly added emails. Silence
      # the Slack messages to avoid spam. Can leave user_id blank since we're
      # silencing the Slack notifications
      support_add_comment(channel.id, case_num, response, "",
                          "Auto Asset Subscription", False)


def tracking_check(channel, asset_type, asset_id, case_num):
  asset = channel.collection(asset_type).get()
  for item in asset:
    item_dict = item.to_dict()
    if item_dict["asset_id"] == asset_id:
      # print(item_dict)
      new_emails = support_subscribe_email(item_dict["channel_id"], case_num,
                                           item_dict["cc_list"],
                                           item_dict["user_id"])
      return new_emails
  return []


if __name__ == "__main__":
  case_updates(True)
