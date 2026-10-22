#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_auto.py —— 云端 runner 执行的每日自动化：签到 + 派猫猫旅行，结果推飞书。

设计要点：
  1. 一次运行把签到与派猫都做掉；
  2. 先领已到家的旅行积分，再判断能否派新一趟；今日名额已用则不派；
  3. 签到与猫猫状态分开汇报；
  4. 猫猫失败不污染签到结论 —— 签到成功即整体成功。

凭证来自 GitHub Secret（由本机 refresh_token.py 注入），只读环境变量，不落盘、不打日志。
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
REQUEST_TIMEOUT = 20
MAX_RETRY = 3

CODE_NO_NETWORK = -1


def log(msg):
    print(msg, flush=True)


def masked(text):
    """把返回体里的 token 类字段脱敏后再落日志。"""
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(v, (dict, list)):
                out[k] = masked(v)
            elif isinstance(v, str) and len(v) > 24:
                out[k] = v[:6] + "***" + v[-4:]
            else:
                out[k] = v
        return out
    if isinstance(text, list):
        return [masked(x) for x in text]
    if isinstance(text, str) and len(text) > 200:
        return text[:200] + "..."
    return text


def dig(obj, *keys):
    """逐层取值，任一层缺失返回 None。"""
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request(url, headers, method="GET", payload=None, retry=True):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, {"raw": raw[:300]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"raw": raw[:300]}
            # 偶发 3xx（快速轮询时网关会短暂跳转）不是业务结论，退避后重试
            if 300 <= e.code < 400 and attempt < MAX_RETRY:
                log("  · 收到 %d 跳转，%.1fs 后重试（%d/%d）"
                    % (e.code, 2.0 * attempt, attempt, MAX_RETRY))
                import time
                time.sleep(2.0 * attempt)
                continue
            return e.code, body
        except Exception as e:
            # 只对网络抖动与 5xx 重试；写操作靠接口幂等，最多重试一次
            if not retry or attempt >= MAX_RETRY:
                return CODE_NO_NETWORK, {"error": "%s: %s" % (type(e).__name__, e)}
            log("  · 请求异常，%.1fs 后重试（%d/%d）" % (2.0, attempt, MAX_RETRY))
            import time
            time.sleep(2.0)


def get(url, headers):
    return request(url, headers, method="GET")


def post(url, headers, payload=None):
    return request(url, headers, method="POST", payload=payload)


def api_ok(code, body):
    return 200 <= code < 300 and (not isinstance(body, dict) or body.get("code", 0) == 0)


def unwrap(body):
    """真实返回统一是 {code,msg,data} 信封，业务字段都在 data 里。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- 签到

def run_checkin(headers, endpoint):
    """查状态 → 未签才领。返回 (ok, 汇报 dict)。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
    log("  [签到-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))

    if scode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "网络不可达，签到未完成"}
    if scode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s），请重新运行 refresh_token.py 注入新 token" % scode}
    if not (200 <= scode < 300):
        return False, {"result": "ERROR", "report": "状态接口异常（HTTP %s）" % scode}

    status = unwrap(sbody)
    if dig(status, "active") is False:
        name = dig(status, "activity_name")
        return True, {"result": "INACTIVE", "report": "签到活动未开启" + ("（%s）" % name if name else "")}

    if dig(status, "today_checked_in") in (True, 1):
        return True, {"result": "ALREADY", "report": _fmt_already(status)}

    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers)
    log("  [签到-领取] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))

    if ccode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "领取请求未送达，签到未完成"}
    if ccode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s）" % ccode}

    if _already_checked_in(ccode, cbody):
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        return True, {"result": "ALREADY",
                      "report": _fmt_already(unwrap(fresh), via="今日已签过（服务端判定已领取）")}

    credit = dig(unwrap(cbody), "credit")
    if credit is not None:
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        s = unwrap(fresh)
        bonus = "，且为连签奖励日" if dig(s, "is_streak_day") else ""
        streak = dig(s, "streak_days")
        cum = dig(s, "total_credits")
        tail = ""
        if streak is not None:
            tail = "（连续 %s 天%s）" % (streak, "，累计 %s 积分" % cum if cum is not None else "")
        return True, {"result": "CLAIMED", "credit": credit,
                      "report": "成功领取 %s 积分%s%s" % (credit, bonus, tail)}

    if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
        return False, {"result": "ERROR",
                       "report": "领取失败：%s（HTTP %s）" % (cbody.get("msg") or cbody.get("code"), ccode)}
    return False, {"result": "UNKNOWN", "report": "未识别的领取返回"}


def _already_checked_in(code, body):
    if isinstance(body, dict):
        msg = body.get("msg") or ""
        if body.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _fmt_eta(arrive_at, server_now):
    """把秒级时间戳差格式化成人类可读文案；数据缺失时不编造。"""
    if arrive_at is None or server_now is None:
        return ""
    try:
        left = int(arrive_at) - int(server_now)
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "，已到家待领"
    if left < 3600:
        return "，约 %d 分钟后到家" % max(1, left // 60)
    return "，约 %.1f 小时后到家" % (left / 3600)


def _fmt_already(status, via="今日已签过"):
    parts = []
    if dig(status, "today_credit") is not None:
        parts.append("今日 +%s" % dig(status, "today_credit"))
    if dig(status, "streak_days") is not None:
        parts.append("连续 %s 天" % dig(status, "streak_days"))
    if dig(status, "total_credits") is not None:
        parts.append("累计 %s 积分" % dig(status, "total_credits"))
    return "%s（%s）" % (via, "，".join(parts)) if parts else via


# ---------------------------------------------------------------- 猫猫

def run_buddy(headers, endpoint):
    """先领已到家的旅行积分，再判断能否派新一趟。猫猫失败不影响签到结论。"""
    base = endpoint + "/v2/activity/growth"
    notes = []
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        log("  [猫猫-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))

        if scode == CODE_NO_NETWORK:
            return {"result": "NETWORK", "report": "网络不可达，猫猫未处理", "notes": notes}
        if scode in (401, 403):
            return {"result": "AUTH", "report": "猫猫认证被拒（HTTP %s）" % scode, "notes": notes}
        if not (200 <= scode < 300):
            return {"result": "ERROR", "report": "旅行状态接口异常（HTTP %s）" % scode, "notes": notes}

        raw_state = dig(unwrap(sbody), "state")
        travel = raw_state
        daily_limit = bool(dig(unwrap(sbody), "daily_limit_reached"))
        travel_body = unwrap(sbody)

        # 1) 已到家 → 先领积分
        if travel == "arrived":
            ccode, cbody = post(base + "/buddy/travel/claim", headers,
                                {"record_id": dig(travel_body, "record_id")})
            log("  [猫猫-领礼物] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
            got = dig(unwrap(cbody), "reward_credit")
            if api_ok(ccode, cbody) and got is not None:
                notes.append("领旅行礼物 +%s 积分" % got)
                travel = "idle"
            else:
                return {"result": "ERROR",
                        "report": "领旅行礼物失败：%s（HTTP %s）" % (dig(cbody, "msg") or "未知", ccode),
                        "notes": notes}
        elif travel == "traveling":
            loc = dig(travel_body, "location", "name") or "?"
            notes.append("旅行中（%s%s）" % (loc, _fmt_eta(dig(travel_body, "arrive_at"),
                                                          dig(travel_body, "server_now"))))

        # 2) 判断能否派新一趟
        if travel == "traveling":
            return {"result": "IN_TRIP", "notes": notes,
                    "report": "；".join(notes) if notes else "猫猫在旅行中，无需派出"}

        if travel == "idle":
            if daily_limit:
                return {"result": "LIMIT_REACHED",
                        "report": "；".join(notes + ["今日旅行名额已用完，不再派出"])}
            kcode, kbody = get(base + "/buddy/travel/config", headers)
            log("  [猫猫-目的地] HTTP %s %s" % (kcode, json.dumps(masked(kbody), ensure_ascii=False)))
            locs = dig(unwrap(kbody), "locations") if 200 <= kcode < 300 else None
            if not locs or not isinstance(locs[0], dict):
                return {"result": "ERROR", "report": "未取到可派目的地", "notes": notes}
            loc = locs[0]
            dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                {"location_id": loc.get("id")})
            log("  [猫猫-派出] HTTP %s %s" % (dcode, json.dumps(masked(dbody), ensure_ascii=False)))
            if 200 <= dcode < 300:
                depart = unwrap(dbody)
                name = dig(depart, "location", "name") or loc.get("name") or "?"
                dur = dig(depart, "duration_hours") or dig(depart, "location", "duration_hours")
                notes.append("已派猫猫去%s%s" % (name, "（约 %s 小时后回）" % dur if dur else ""))
                return {"result": "DEPARTED", "notes": notes, "report": "；".join(notes)}
            msg = dig(unwrap(dbody), "msg") or dig(dbody, "msg") or ""
            # 名额已用完属于正常幂等结果，不算失败
            if "limit" in str(msg).lower() or dcode == 429:
                notes.append("今日旅行名额已用完，不再派出")
                return {"result": "LIMIT_REACHED", "notes": notes, "report": "；".join(notes)}
            return {"result": "ERROR",
                    "report": "派出失败：%s（HTTP %s）" % (msg or "未知", dcode), "notes": notes}

        # 兜底：state 不是已知取值。原样汇报、不做写操作，避免误派或误领。
        return {"result": "UNKNOWN_STATE",
                "report": "；".join(notes) if notes else "未识别的旅行状态：%s" % raw_state,
                "notes": notes}
    except Exception as e:
        return {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e), "notes": notes}


# ---------------------------------------------------------------- 飞书

def push_feishu(webhook, title, checkin_line, buddy_line, overall_ok):
    """推送到飞书自定义机器人。返回 (ok, reason)。

    重试策略与业务请求、WPS 版保持一致：
      - 网络异常 / HTTP 5xx → 自动重试（最多 MAX_RETRY 次，退避），瞬时故障自愈
      - 飞书业务拒绝（19001 地址无效、机器人开启签名校验等）→ 不重试（重试无用），
        原因带改法返回，写入结果供 step summary 展示
    兜底：脚本幂等，当天 cron 有多个时段时，下一次运行会自动再推一遍当天结果。
    """
    color = "green" if overall_ok else "red"
    lines = [
        "**%s**" % title,
        "",
        "**签到**",
        checkin_line,
        "",
        "**猫猫旅行**",
        buddy_line,
    ]
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": color,
                "title": {"tag": "plain_text",
                          "content": "Buddy 加油站 · %s" % ("成功" if overall_ok else "部分失败")},
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        },
    }
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(webhook, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                log("  [飞书] HTTP %s %s" % (resp.status, json.dumps(body, ensure_ascii=False)))
                if body.get("code") == 0:
                    return True, ""
                return False, ("飞书返回码 %s（19001 多为地址不对；机器人若开了签名校验也会失败。"
                               "这是配置问题，重试无用：检查 Secret FEISHU_WEBHOOK，或建机器人时"
                               "不要勾选签名校验）" % body.get("code", "未知"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log("  [飞书] HTTP %s，退避后重试（%d/%d）" % (e.code, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            if e.code >= 500:
                return False, "重试 %d 次后仍 HTTP %s" % (MAX_RETRY, e.code)
            return False, "HTTP %s" % e.code
        except Exception as e:
            if attempt < MAX_RETRY:
                log("  [飞书] 请求异常，退避后重试（%d/%d）：%s: %s"
                    % (attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(2.0 * attempt)
                continue
            return False, "重试 %d 次后仍异常：%s: %s" % (MAX_RETRY, type(e).__name__, e)


# ---------------------------------------------------------------- 主流程

def main():
    token = os.environ.get("WB_ACCESS_TOKEN")
    webhook = os.environ.get("FEISHU_WEBHOOK")
    endpoint = os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT

    if not token:
        log("缺少环境变量 WB_ACCESS_TOKEN（请在本机运行 refresh_token.py 注入 GitHub Secret）")
        return 2
    if not webhook:
        log("缺少环境变量 FEISHU_WEBHOOK")

    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
    }

    log("=== 1/2 签到 ===")
    checkin_ok, checkin = run_checkin(headers, endpoint)
    log("  结论：%s" % checkin["report"])

    # 猫猫独立 try：任何异常都不影响签到结论
    log("=== 2/2 猫猫旅行 ===")
    try:
        buddy = run_buddy(headers, endpoint)
    except Exception as e:
        buddy = {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e)}
    log("  结论：%s" % buddy.get("report", ""))

    # 整体成败只看签到：签到成功即成功
    overall_ok = checkin_ok
    checkin_line = ("✅ " if checkin_ok else "❌ ") + checkin["report"]
    buddy_line = ("🐱 " + buddy.get("report", "")) if buddy.get("report") else "🐱 无结论"

    # 推送状态与业务结论分开呈现：失败原因可诊断，但不污染签到结论
    push_ok, push_reason = None, "未配置（缺少 FEISHU_WEBHOOK，本次不推送）"
    if webhook:
        push_ok, push_reason = push_feishu(webhook, "每日自动化", checkin_line, buddy_line, overall_ok)

    result = {
        "checkin": {"ok": checkin_ok, **checkin},
        "buddy": {k: v for k, v in buddy.items() if k != "notes"},
        "push": {"ok": push_ok, "reason": push_reason},
        "overall_ok": overall_ok,
    }
    print("RESULT_JSON " + json.dumps(result, ensure_ascii=False))

    # 结果文件只含结论文案，不含 token，供 Actions 写入 step summary
    result_file = os.environ.get("WB_RESULT_FILE")
    if result_file:
        try:
            push_line = ("✅ 已推送到飞书" if push_ok
                         else "❌ 推送失败：%s" % push_reason)
            with open(result_file, "w", encoding="utf-8") as f:
                f.write("| 项目 | 结论 |\n| --- | --- |\n")
                f.write("| 签到 | %s |\n" % checkin_line)
                f.write("| 猫猫旅行 | %s |\n" % buddy_line)
                f.write("| 飞书推送 | %s |\n" % push_line)
                f.write("| 整体 | %s |\n" % ("成功（签到已达成）" if overall_ok else "失败（签到未达成）"))
        except OSError as e:
            log("结果文件写入失败：%s" % e)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())