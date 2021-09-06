import base64
import glob
import os
import pathlib
import re
import time
from datetime import datetime

import cv2 as cv
import imutils
import numpy as np
from appium.webdriver.common.touch_action import TouchAction
from appium.webdriver.webdriver import WebDriver
from selenium.common.exceptions import TimeoutException
from typing import List, Iterator

SIMILARITY_ORDER = 0
VERTICAL_ORDER = 1
HORIZONTAL_ORDER = 2

CV_CCOEFF = cv.TM_CCOEFF_NORMED
CV_CCORR = cv.TM_CCORR_NORMED
CV_SQDIFF = cv.TM_SQDIFF_NORMED


class VisualMatch(object):

    def __init__(self, driver, x, y, width, height, similarity):
        self.driver = driver
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.similarity = similarity

    def click(self):
        TouchAction(self.driver).press(x=self.x, y=self.y).release().perform()


class VisualElement(object):

    def __init__(self, driver: WebDriver, path, order: int = 0, disposal: int = SIMILARITY_ORDER,
                 similarity: float = 0.7, method=CV_CCOEFF, name: str = None):
        self.driver = driver
        self.path = str(path)
        self.order = order
        self.disposal = disposal
        self.name = name or re.sub(r'\W+', '_', pathlib.Path(path).stem)
        self.similarity = similarity
        self.method = method

        self.max_occurrences = 16
        self._debug = False
        self._searched = False
        self.matches: List[VisualMatch] = []

    @property
    def match(self) -> VisualMatch:
        return self[0]

    def __len__(self) -> int:
        return len(self.matches if self._searched else self._search_all_matches().matches)

    def __getitem__(self, i) -> VisualMatch:
        try:
            return (self.matches if self._searched else self._search_all_matches().matches)[i]
        except IndexError:
            raise Exception(f"Index above matches: {self.order} > {len(self.matches)}")

    def __iter__(self) -> Iterator[VisualMatch]:
        return iter(self.matches if self._searched else self._search_all_matches().matches)

    def debug(self, enable=True):
        self._debug = enable
        return self

    def reset_object(self):
        self._searched = False
        self.matches.clear()
        return self

    def is_visible(self):
        return bool(self._search_all_matches().matches)

    def wait_until_visible(self, timeout=10):
        start_time = datetime.now()
        while not self.is_visible():
            time_delta = datetime.now() - start_time
            if time_delta.total_seconds() >= timeout:
                raise TimeoutException(f'Cannot find "{self.name}" in {timeout} seconds')
            time.sleep(1)
        return self

    def wait_until_not_visible(self, timeout=10):
        start_time = datetime.now()
        while self.is_visible():
            time_delta = datetime.now() - start_time
            if time_delta.total_seconds() >= timeout:
                raise TimeoutException(f'Still present "{self.name}" in {timeout} seconds')
            time.sleep(1)
        return self

    def wait_until_clickable(self, timeout=10):
        return self.wait_until_visible(timeout)

    def click(self, timeout=10):
        self.wait_until_clickable(timeout)[self.order].click()
        return self

    @staticmethod
    def _get_bounds(loc, w, h, r):
        start_x, start_y = (int(loc[0] * r), int(loc[1] * r))
        end_x, end_y = (int((loc[0] + w) * r), int((loc[1] + h) * r))
        return start_x, start_y, end_x, end_y

    def _search_all_matches(self):
        self.reset_object()

        for template_path in glob.glob(self.path):
            self._find_matches(template_path)

        if self.disposal == VERTICAL_ORDER:
            self.matches.sort(key=lambda v: v.y)
        elif self.disposal == HORIZONTAL_ORDER:
            self.matches.sort(key=lambda v: v.x)
        elif self.disposal == SIMILARITY_ORDER:
            self.matches.sort(key=lambda v: v.similarity, reverse=True)  # more similar first

        self._searched = True
        return self

    def _find_matches(self, template_path):
        if not os.path.isfile(template_path):
            raise Exception(f"Template route doesn't exist: {template_path}")
        if self.disposal not in range(2):
            raise Exception(f"Disposal priority not valid: {self.disposal}")
        if self.order not in range(self.max_occurrences):
            raise Exception(f"Order above max occurrences: {self.order} > {self.max_occurrences}")

        # load the template image, convert it to grayscale
        template = cv.imread(template_path)
        template = cv.cvtColor(template, cv.COLOR_BGR2GRAY)
        h, w = template.shape[:2]

        # load the image and initialize the bookkeeping variable to keep track of the matched region
        screenshot = self.driver.get_screenshot_as_base64()
        np_data = np.fromstring(base64.b64decode(screenshot), np.uint8)  # noqa - it accepts bytes
        image = cv.imdecode(np_data, cv.IMREAD_UNCHANGED)

        if self._debug:
            os.makedirs('temp', exist_ok=True)
            cv.imwrite(f'temp/velenium_{int(time.time() * 10**6)}__{self.name}__screenshot.png', image)

        gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
        occurrence = None

        # loop over the scales of the image
        for scale in np.linspace(0.2, 1.0, 20)[::-1]:

            # resize the image according to the scale, and keep track of the ratio of the resizing
            resized = imutils.resize(gray, width=int(gray.shape[1] * scale))
            r = gray.shape[1] / float(resized.shape[1])

            # if the resized image is smaller than the template, then break from the loop
            if resized.shape[0] < h or resized.shape[1] < w:
                break

            # apply template matching to find the template in the image
            result = cv.matchTemplate(resized, template, self.method)

            # get best match and its coordinates, discard if not enough similarity
            _, max_val, _, max_loc = cv.minMaxLoc(result)
            if max_val <= self.similarity:
                continue

            # if we have found a new maximum correlation value, then update the bookkeeping variable
            if occurrence is None or max_val > occurrence[0]:
                occurrence = (max_val, max_loc, r, resized)

            # draw a bounding box around the detected region
            if self._debug:
                clone = np.dstack([resized, resized, resized])
                cv.rectangle(clone, (max_loc[0], max_loc[1]), (max_loc[0] + w, max_loc[1] + h), (0, 0, 255), 2)
                cv.imwrite(f'temp/velenium_{int(time.time() * 10**6)}__{self.name}__{int(max_val * 100)}.png', clone)

        if not occurrence:
            return

        # unpack the bookkeeping variable and compute the coordinates of the bounding box based on the resized ratio
        max_val, max_loc, r, resized = occurrence
        start_x, start_y, end_x, end_y = self._get_bounds(max_loc, w, h, r)

        # draw a bounding box around the detected result and display the image
        if self._debug:
            cv.rectangle(image, (start_x, start_y), (end_x, end_y), (0, 0, 255), 2)
            cv.imwrite(f'temp/velenium_{int(time.time() * 10**6)}__{self.name}__best.png', image)

        self.matches += [VisualMatch(self.driver, start_x + int(w / 2), start_y + int(h / 2), w, h, max_val)]

        # from that resized image get
        for i in range(self.max_occurrences):

            # cover the previous best match in black to discard duplicates
            cv.rectangle(resized, (max_loc[0], max_loc[1]), (max_loc[0] + w, max_loc[1] + h), (0, 0, 0), -1)

            if self._debug:
                cv.imwrite(f'temp/velenium_{int(time.time() * 10**6)}__{self.name}__covered_{i}.png', resized)

            # repeat template matching to find the template in the image
            result = cv.matchTemplate(resized, template, self.method)
            _, max_val, _, max_loc = cv.minMaxLoc(result)
            if max_val <= self.similarity:
                break

            start_x, start_y, end_x, end_y = self._get_bounds(max_loc, w, h, r)
            self.matches.append(VisualMatch(self.driver, start_x + int(w / 2), start_y + int(h / 2), w, h, max_val))
            if self._debug:
                cv.rectangle(image, (start_x, start_y), (end_x, end_y), (0, 0, 255), 2)

        if self._debug:
            cv.imwrite(f'temp/velenium_{int(time.time() * 10**6)}__{self.name}__all.png', image)
