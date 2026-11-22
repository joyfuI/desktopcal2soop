"""SOOP web calendar requests and portable configuration."""

import base64
import configparser
import json
import os
import re
import tempfile
from datetime import date

import requests

from calendar_source import DEFAULT_EVENT_TIME, dpapi

ORIGIN = "https://www.sooplive.com"
LOGIN_ORIGIN = "https://login.sooplive.com"
LOGIN_PAGE = f"{LOGIN_ORIGIN}/afreeca/login.php"
LOGIN_ACTION = f"{LOGIN_ORIGIN}/app/LoginAction.php"
ACCOUNT = re.compile(r"[a-zA-Z0-9_]{1,40}\Z")
LOGIN_RESULT = re.compile(rb'\bRESULT["\x27]?\s*:\s*["\x27]?(-?\d+)')


class SoopError(RuntimeError):
    def __init__(self, message, *, ambiguous=False):
        super().__init__(message)
        self.ambiguous = ambiguous


class SoopLoginBlocked(SoopError):
    pass


class SoopEventNotFound(SoopError):
    pass


def seal(data):
    return "dpapi:" + base64.b64encode(dpapi(data, protect=True)).decode("ascii")


def unseal(value):
    if not value.startswith("dpapi:"):
        raise ValueError("INI 인증 정보가 올바르지 않습니다.")
    return dpapi(base64.b64decode(value[6:], validate=True))


def load_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.read_dict({"soop": {"username": "", "password": ""},
                      "sync": {"interval_minutes": "10", "default_time": DEFAULT_EVENT_TIME},
                      "session": {"auth": ""}})
    if not path.exists():
        try:
            save_config(path, config, create_only=True)
        except FileExistsError:
            pass
    with path.open(encoding="utf-8-sig") as stream:
        config.read_file(stream)
    return config


def save_config(path, config, *, create_only=False):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".config-", suffix=".tmp", delete=False) as stream:
        temp = str(stream.name)
        try:
            config.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            if not create_only:
                os.replace(temp, path)
            elif os.name == "nt":
                os.rename(temp, path)
            else:
                os.link(temp, path)
        finally:
            stream.close()
            if os.path.exists(temp):
                os.unlink(temp)


def valid_domain(domain):
    domain = domain.lstrip(".").lower()
    return domain == "sooplive.com" or domain.endswith(".sooplive.com")


class SoopClient:
    def __init__(self, path):
        self.path = path
        config = load_config(path)
        self.user = config["soop"]["username"].strip()
        self.password = config["soop"]["password"]
        if not ACCOUNT.fullmatch(self.user) or not self.password:
            raise SoopLoginBlocked("config.ini의 [soop]에 SOOP 아이디와 비밀번호를 입력하세요.")
        self.calendar = f"{ORIGIN}/station/{self.user}/calendar"
        self.api = f"https://api-channel.sooplive.com/v1.1/channel/{self.user}/calendar"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"})
        self.logged_in_this_run = False
        try:
            if config["session"]["auth"]:
                saved = json.loads(unseal(config["session"]["auth"]))
                if saved["username"] == self.user:
                    for cookie in saved["cookies"]:
                        if valid_domain(cookie["domain"]):
                            self.session.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
        except (OSError, ValueError, TypeError, KeyError):
            self.session.cookies.clear()

    def _request(self, method, address, **kwargs):
        try:
            return self.session.request(method, address, timeout=(5, 15), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SoopError("SOOP에 연결하지 못했습니다. 네트워크 상태를 확인하세요.",
                            ambiguous=method in ("POST", "PUT", "DELETE")) from None

    def _login(self):
        if self.logged_in_this_run:
            raise SoopError("SOOP 인증 갱신 후에도 인증이 거부됐습니다.")
        self.logged_in_this_run = True
        self.session.cookies.clear()
        page = self._request("GET", LOGIN_PAGE, params={"request_uri": self.calendar, "szFrom": "full"})
        if page.status_code != 200:
            raise SoopError(f"SOOP 로그인 화면을 열 수 없습니다 (HTTP {page.status_code}).")
        response = self._request("POST", LOGIN_ACTION, data={
            "szWork": "login", "szType": "json", "szUid": self.user, "szPassword": self.password,
            "isSaveId": "false", "szScriptVar": "oLoginRet", "szAction": "", "isLoginRetain": "Y",
        }, headers={"Origin": LOGIN_ORIGIN, "Referer": page.url, "X-Requested-With": "XMLHttpRequest"})
        match = LOGIN_RESULT.search(response.content)
        if response.status_code != 200 or not match:
            raise SoopError(f"SOOP 로그인 응답을 확인할 수 없습니다 (HTTP {response.status_code}).")
        code = int(match[1])
        if code in (-11, -12):
            raise SoopLoginBlocked("SOOP에서 추가 인증을 요구합니다. 브라우저에서 계정 상태를 확인하세요.")
        if code != 1 or not self._has_ticket():
            raise SoopLoginBlocked(f"SOOP 로그인 실패 (결과 {code}). 계정 설정을 확인하세요.")

        cookies = [{key: getattr(cookie, key) for key in ("name", "value", "domain", "path", "secure", "expires")}
                   for cookie in self.session.cookies if valid_domain(cookie.domain)]
        config = load_config(self.path)
        if (config["soop"]["username"].strip(), config["soop"]["password"]) != (self.user, self.password):
            raise SoopError("동기화 중 SOOP 계정 설정이 변경됐습니다. 다시 실행하세요.")
        config["session"]["auth"] = seal(json.dumps({"username": self.user, "cookies": cookies}).encode("utf-8"))
        save_config(self.path, config)

    def _has_ticket(self):
        return any(cookie.name == "AuthTicket" and cookie.value and not cookie.is_expired()
                   for cookie in self.session.cookies)

    def calendar_request(self, method, *, event_id=None, params=None, data=None, payload=None):
        if not self._has_ticket():
            self._login()
        address = self.api if event_id is None else f"{self.api}/{int(event_id)}"
        kwargs = {"params": params, "data": data, "json": payload,
                  "headers": {"Origin": ORIGIN, "Referer": f"{ORIGIN}/"}}
        response = self._request(method, address, **kwargs)
        if response.status_code == 401 and not self.logged_in_this_run:
            self._login()
            response = self._request(method, address, **kwargs)
        if event_id is not None and response.status_code == 404:
            raise SoopEventNotFound("SOOP 일정 요청에 실패했습니다 (HTTP 404).")
        if method == "DELETE" and response.status_code == 204:
            return {}
        if method == "DELETE" and 200 <= response.status_code < 300:
            raise SoopError("SOOP 일정 삭제 응답을 확인할 수 없습니다.", ambiguous=True)
        try:
            result = response.json()
        except ValueError:
            result = None
        if not 200 <= response.status_code < 300 or not isinstance(result, dict) or result.get("code") not in (None, 0, "0"):
            code = result.get("code") if isinstance(result, dict) else None
            if code == "CHA0002":
                raise SoopError("SOOP 일정 제목이 길이 제한을 초과했습니다.")
            if response.status_code in (401, 403):
                raise SoopError(f"SOOP 캘린더가 인증 또는 권한을 거부했습니다 (HTTP {response.status_code}).")
            raise SoopError(f"SOOP 캘린더 {method} 실패 (HTTP {response.status_code}).",
                            ambiguous=response.status_code >= 500 or response.ok)
        return result

    def list_events(self, day):
        value = date.fromisoformat(day)
        result = self.calendar_request("GET", params={"view": "month", "year": value.year,
                                                      "month": value.month, "day": value.day, "userId": self.user})
        try:
            days = result["days"]
            if not isinstance(days, list) or any(not isinstance(row["events"], list) for row in days):
                raise ValueError
            return [event for row in days for event in row["events"]]
        except (KeyError, TypeError, ValueError):
            raise SoopError("SOOP 일정 목록 형식을 확인할 수 없습니다.") from None

    def fields(self, event):
        return {"userId": self.user, "title": event.title, "calendarType": event.calendar_type, "eventTime": event.time}

    def create(self, event):
        result = self.calendar_request("POST", data={**self.fields(event), "pickDates[]": event.day})
        try:
            created, = result["events"]
            return int(created["idx"])
        except (KeyError, TypeError, ValueError):
            raise SoopError("SOOP에서 생성된 일정 ID를 확인할 수 없습니다.", ambiguous=True) from None

    def update(self, event_id, event):
        result = self.calendar_request("PUT", event_id=event_id, payload={**self.fields(event), "calendarNo": event_id,
                                                                          "startDate": event.day})
        if not any(row.get("idx") in (event_id, str(event_id)) for row in result.get("events", [])):
            raise SoopError("SOOP에서 수정한 일정 ID를 확인할 수 없습니다.", ambiguous=True)

    def delete(self, event_id):
        self.calendar_request("DELETE", event_id=event_id, payload={"userId": self.user, "eventId": event_id})
