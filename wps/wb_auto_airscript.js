/**
 * wb_auto_airscript.js —— WPS AirScript 版每日自动化：签到 + 派猫猫旅行。
 *
 * 移植自 wb-auto 仓库的 wb_auto.py，逻辑完全一致：
 *   1. 先查签到状态，未签才领取（幂等：已签不重领）；
 *   2. 猫猫先领已到家的旅行积分，再判断能否派新一趟（幂等：名额用完不重派）；
 *   3. 签到与猫猫分开汇报，猫猫失败不污染签到结论；
 *   4. 结果回写表格 + 推送飞书卡片。
 *
 * 配置（在名为「签到自动化」的工作表里，脚本会自动创建并写表头）：
 *   B1 = WB_ACCESS_TOKEN  （必填，由本机 refresh_token.py 导出，约 28 天更新一次）
 *   B2 = FEISHU_WEBHOOK   （选填，飞书群自定义机器人地址）
 *   B3 = WB_ENDPOINT      （选填，留空即默认 https://copilot.tencent.com）
 *   B5/B6/B7/B8/B9 = 运行后自动回写：签到结论 / 猫猫结论 / 整体 / 上次运行时间 / 飞书推送
 *
 * 使用前提：脚本编辑器「服务」中已添加「网络 API」。
 * 兼容性：同时兼容 AirScript 2.0（ET JSAPI：Value 是方法、Value2 可写、
 * Worksheets.Add 四参）与 1.0（databook：Value 可写属性、Sheets.Add 五参、
 * Application.Enum），全部走探测降级，不依赖任一特定实现。
 */

var ENDPOINT_DEFAULT = "https://copilot.tencent.com";
var SHEET_NAME = "签到自动化";
var XL_WORKSHEET = -4167; // XlSheetType.xlWorksheet
var MAX_RETRY = 3;
var NO_NETWORK = -1;

// ---------------------------------------------------------------- 基础工具

function log(msg) {
  console.log(msg);
}

/** 逐层取值：dig(obj, "a", "b", "c") 等价于 obj.a.b.c，任一层缺失返回 null。 */
function dig() {
  var obj = arguments[0];
  for (var i = 1; i < arguments.length; i++) {
    var k = arguments[i];
    if (obj && typeof obj === "object" && obj[k] !== undefined && obj[k] !== null) {
      obj = obj[k];
    } else {
      return null;
    }
  }
  return obj;
}

/** 把返回体里的长字符串脱敏后再落日志（防止 token 类字段进日志）。 */
function masked(text) {
  if (text && typeof text === "object" && !Array.isArray(text)) {
    var out = {};
    for (var k in text) {
      if (Object.prototype.hasOwnProperty.call(text, k)) {
        out[k] = masked(text[k]);
      }
    }
    return out;
  }
  if (Array.isArray(text)) {
    var arr = [];
    for (var i = 0; i < text.length; i++) arr.push(masked(text[i]));
    return arr;
  }
  if (typeof text === "string" && text.length > 24) {
    return text.slice(0, 6) + "***" + text.slice(-4);
  }
  return text;
}

function parseJson(raw) {
  try {
    return JSON.parse(raw);
  } catch (e) {
    return { raw: String(raw).slice(0, 300) };
  }
}

function asInt(value, defaultValue) {
  var n = parseInt(value, 10);
  return isNaN(n) ? (defaultValue || 0) : n;
}

/** 统一请求入口：带重试（3xx 跳转、5xx、网络异常），返回 [code, body]。 */
function request(method, url, headers, payload) {
  var attempt = 0;
  while (true) {
    attempt++;
    try {
      var opts = { method: method, timeout: 15000, headers: headers };
      if (method === "POST" && payload !== null && payload !== undefined) {
        opts.body = JSON.stringify(payload);
      }
      var resp = HTTP.fetch(url, opts);
      var code = resp.status;
      var body;
      try {
        body = resp.json();
      } catch (eJson) {
        try {
          body = parseJson(resp.text());
        } catch (eText) {
          body = { raw: String(eText).slice(0, 200) };
        }
      }
      // 偶发 3xx（网关短暂跳转）与 5xx 不是业务结论，退避后重试
      if (attempt < MAX_RETRY && ((code >= 300 && code < 400) || code >= 500)) {
        log("  · 收到 HTTP " + code + "，退避后重试（" + attempt + "/" + MAX_RETRY + "）");
        Time.sleep(1500 * attempt);
        continue;
      }
      return [code, body];
    } catch (e) {
      if (attempt >= MAX_RETRY) {
        return [NO_NETWORK, { error: String(e) }];
      }
      log("  · 请求异常，退避后重试（" + attempt + "/" + MAX_RETRY + "）：" + String(e));
      Time.sleep(1500);
    }
  }
}

function get(url, headers) {
  return request("GET", url, headers, null);
}

function post(url, headers, payload) {
  return request("POST", url, headers, payload === undefined ? null : payload);
}

function apiOk(code, body) {
  if (code < 200 || code >= 300) return false;
  if (body && typeof body === "object" && body.code !== undefined && body.code !== 0) return false;
  return true;
}

/** 真实返回统一是 {code,msg,data} 信封，业务字段都在 data 里。 */
function unwrap(body) {
  if (body && typeof body === "object" && body.data && typeof body.data === "object") {
    return body.data;
  }
  return body && typeof body === "object" ? body : {};
}

// ---------------------------------------------------------------- 签到

function fmtAlready(status, via) {
  if (!via) via = "今日已签过";
  var parts = [];
  if (dig(status, "today_credit") !== null) parts.push("今日 +" + dig(status, "today_credit"));
  if (dig(status, "streak_days") !== null) parts.push("连续 " + dig(status, "streak_days") + " 天");
  if (dig(status, "total_credits") !== null) parts.push("累计 " + dig(status, "total_credits") + " 积分");
  return parts.length ? via + "（" + parts.join("，") + "）" : via;
}

function alreadyCheckedIn(code, body) {
  if (body && typeof body === "object") {
    var msg = body.msg || "";
    if (body.code === 10001 || msg.indexOf("已签") >= 0) return true;
  }
  return false;
}

function runCheckin(headers, endpoint) {
  var r = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers);
  var scode = r[0], sbody = r[1];
  log("  [签到-状态] HTTP " + scode + " " + JSON.stringify(masked(sbody)));

  if (scode === NO_NETWORK) {
    return [false, { result: "NETWORK", report: "网络不可达，签到未完成" }];
  }
  if (scode === 401 || scode === 403) {
    return [false, { result: "AUTH", report: "认证被拒（HTTP " + scode + "），请更新表格 B1 里的 token" }];
  }
  if (scode < 200 || scode >= 300) {
    return [false, { result: "ERROR", report: "状态接口异常（HTTP " + scode + "）" }];
  }

  var status = unwrap(sbody);
  if (dig(status, "active") === false) {
    var name = dig(status, "activity_name");
    return [true, { result: "INACTIVE", report: "签到活动未开启" + (name ? "（" + name + "）" : "") }];
  }

  var checked = dig(status, "today_checked_in");
  if (checked === true || checked === 1) {
    return [true, { result: "ALREADY", report: fmtAlready(status) }];
  }

  var r2 = post(endpoint + "/v2/billing/meter/daily-checkin", headers);
  var ccode = r2[0], cbody = r2[1];
  log("  [签到-领取] HTTP " + ccode + " " + JSON.stringify(masked(cbody)));

  if (ccode === NO_NETWORK) {
    return [false, { result: "NETWORK", report: "领取请求未送达，签到未完成" }];
  }
  if (ccode === 401 || ccode === 403) {
    return [false, { result: "AUTH", report: "认证被拒（HTTP " + ccode + "）" }];
  }

  if (alreadyCheckedIn(ccode, cbody)) {
    var rf = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers);
    return [true, { result: "ALREADY", report: fmtAlready(unwrap(rf[1]), "今日已签过（服务端判定已领取）") }];
  }

  var credit = dig(unwrap(cbody), "credit");
  if (credit !== null && credit !== undefined) {
    var rf2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers);
    var s = unwrap(rf2[1]);
    var bonus = dig(s, "is_streak_day") ? "，且为连签奖励日" : "";
    var streak = dig(s, "streak_days");
    var cum = dig(s, "total_credits");
    var tail = "";
    if (streak !== null) {
      tail = "（连续 " + streak + " 天" + (cum !== null ? "，累计 " + cum + " 积分" : "") + "）";
    }
    return [true, { result: "CLAIMED", report: "成功领取 " + credit + " 积分" + bonus + tail }];
  }

  if (cbody && typeof cbody === "object" && (cbody.code !== undefined || cbody.msg !== undefined)) {
    return [false, { result: "ERROR", report: "领取失败：" + (cbody.msg || cbody.code) + "（HTTP " + ccode + "）" }];
  }
  return [false, { result: "UNKNOWN", report: "未识别的领取返回" }];
}

// ---------------------------------------------------------------- 猫猫

function fmtEta(arriveAt, serverNow) {
  if (arriveAt === null || serverNow === null) return "";
  var left = asInt(arriveAt, NaN) - asInt(serverNow, NaN);
  if (isNaN(left)) return "";
  if (left <= 0) return "，已到家待领";
  if (left < 3600) return "，约 " + Math.max(1, Math.floor(left / 60)) + " 分钟后到家";
  return "，约 " + (left / 3600).toFixed(1) + " 小时后到家";
}

function joinNotes(notes) {
  return notes.length ? notes.join("；") : "";
}

function runBuddy(headers, endpoint) {
  var base = endpoint + "/v2/activity/growth";
  var notes = [];
  try {
    var r = get(base + "/buddy/travel/status", headers);
    var scode = r[0], sbody = r[1];
    log("  [猫猫-状态] HTTP " + scode + " " + JSON.stringify(masked(sbody)));

    if (scode === NO_NETWORK) {
      return { result: "NETWORK", report: "网络不可达，猫猫未处理", notes: notes };
    }
    if (scode === 401 || scode === 403) {
      return { result: "AUTH", report: "猫猫认证被拒（HTTP " + scode + "）", notes: notes };
    }
    if (scode < 200 || scode >= 300) {
      return { result: "ERROR", report: "旅行状态接口异常（HTTP " + scode + "）", notes: notes };
    }

    var travelBody = unwrap(sbody);
    var rawState = dig(travelBody, "state");
    var travel = rawState;
    var dailyLimit = dig(travelBody, "daily_limit_reached") ? true : false;

    // 1) 已到家 → 先领积分
    if (travel === "arrived") {
      var rc = post(base + "/buddy/travel/claim", headers,
                    { record_id: dig(travelBody, "record_id") });
      var ccode = rc[0], cbody = rc[1];
      log("  [猫猫-领礼物] HTTP " + ccode + " " + JSON.stringify(masked(cbody)));
      var got = dig(unwrap(cbody), "reward_credit");
      if (apiOk(ccode, cbody) && got !== null && got !== undefined) {
        notes.push("领旅行礼物 +" + got + " 积分");
        travel = "idle";
      } else {
        return { result: "ERROR",
                 report: "领旅行礼物失败：" + (dig(cbody, "msg") || "未知") + "（HTTP " + ccode + "）",
                 notes: notes };
      }
    } else if (travel === "traveling") {
      var loc = dig(travelBody, "location", "name") || "?";
      notes.push("旅行中（" + loc + fmtEta(dig(travelBody, "arrive_at"), dig(travelBody, "server_now")) + "）");
    }

    // 2) 判断能否派新一趟
    if (travel === "traveling") {
      return { result: "IN_TRIP", notes: notes,
               report: joinNotes(notes) || "猫猫在旅行中，无需派出" };
    }

    if (travel === "idle") {
      if (dailyLimit) {
        notes.push("今日旅行名额已用完，不再派出");
        return { result: "LIMIT_REACHED", notes: notes, report: joinNotes(notes) };
      }
      var rk = get(base + "/buddy/travel/config", headers);
      var kcode = rk[0], kbody = rk[1];
      log("  [猫猫-目的地] HTTP " + kcode + " " + JSON.stringify(masked(kbody)));
      var locs = (kcode >= 200 && kcode < 300) ? dig(unwrap(kbody), "locations") : null;
      if (!locs || !Array.isArray(locs) || !locs.length || !locs[0] || typeof locs[0] !== "object") {
        return { result: "ERROR", report: "未取到可派目的地", notes: notes };
      }
      var loc = locs[0];
      var rd = post(base + "/buddy/travel/depart", headers, { location_id: loc.id });
      var dcode = rd[0], dbody = rd[1];
      log("  [猫猫-派出] HTTP " + dcode + " " + JSON.stringify(masked(dbody)));
      if (dcode >= 200 && dcode < 300) {
        var depart = unwrap(dbody);
        var dname = dig(depart, "location", "name") || loc.name || "?";
        var dur = dig(depart, "duration_hours") || dig(depart, "location", "duration_hours");
        notes.push("已派猫猫去" + dname + (dur ? "（约 " + dur + " 小时后回）" : ""));
        return { result: "DEPARTED", notes: notes, report: joinNotes(notes) };
      }
      var msg = dig(unwrap(dbody), "msg") || dig(dbody, "msg") || "";
      // 名额已用完属于正常幂等结果，不算失败
      if (String(msg).toLowerCase().indexOf("limit") >= 0 || dcode === 429) {
        notes.push("今日旅行名额已用完，不再派出");
        return { result: "LIMIT_REACHED", notes: notes, report: joinNotes(notes) };
      }
      return { result: "ERROR", report: "派出失败：" + (msg || "未知") + "（HTTP " + dcode + "）", notes: notes };
    }

    // 兜底：state 不是已知取值。原样汇报、不做写操作，避免误派或误领。
    return { result: "UNKNOWN_STATE", notes: notes,
             report: joinNotes(notes) || "未识别的旅行状态：" + rawState };
  } catch (e) {
    return { result: "ERROR", report: "猫猫模块异常：" + String(e), notes: notes };
  }
}

// ---------------------------------------------------------------- 飞书

/** 推送到飞书自定义机器人。返回 {ok, reason}。
 *
 * 重试策略与业务请求对齐：
 *   - 网络异常 / HTTP 5xx → 自动重试（最多 MAX_RETRY 次，退避），这类失败重试有用
 *   - 飞书业务拒绝（地址无效 19001、签名校验开启等）→ 不重试，重试也无用，原因写进 B9
 * 全部失败后，下一次定时运行会再推一遍当天结果（脚本幂等），相当于天然兜底。
 */
function pushFeishu(webhook, title, checkinLine, buddyLine, overallOk) {
  var color = overallOk ? "green" : "red";
  var lines = [
    "**" + title + "**",
    "",
    "**签到**",
    checkinLine,
    "",
    "**猫猫旅行**",
    buddyLine
  ];
  var payload = {
    msg_type: "interactive",
    card: {
      config: { wide_screen_mode: true },
      header: {
        template: color,
        title: { tag: "plain_text", content: "Buddy 加油站 · " + (overallOk ? "成功" : "部分失败") }
      },
      elements: [{ tag: "div", text: { tag: "lark_md", content: lines.join("\n") } }]
    }
  };
  var attempt = 0;
  while (true) {
    attempt++;
    try {
      var resp = HTTP.fetch(webhook, {
        method: "POST",
        timeout: 15000,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      var body = {};
      try { body = resp.json(); } catch (e) { body = {}; }
      log("  [飞书] 第" + attempt + "次 HTTP " + resp.status + " " + JSON.stringify(body));
      // 5xx 属瞬时故障，退避重试
      if (resp.status >= 500) {
        if (attempt < MAX_RETRY) {
          Time.sleep(2000 * attempt);
          continue;
        }
        return { ok: false, reason: "重试 " + MAX_RETRY + " 次后仍 HTTP " + resp.status };
      }
      if (resp.status >= 300) {
        return { ok: false, reason: "HTTP " + resp.status };
      }
      if (body.code === 0) {
        return { ok: true };
      }
      // 常见：19001 = webhook 地址无效或复制不完整；开启签名校验也会被拒。
      // 这类是配置问题，重试无用，直接带原因返回，由 B9 提示用户改配置。
      return { ok: false,
               reason: "飞书返回码 " + (body.code === undefined ? "未知" : body.code) +
                       "（19001 多为地址不对；机器人若开了签名校验也会失败。这是配置问题，"
                       + "重试无用：检查 B2 地址，或建机器人时不勾选签名校验）" };
    } catch (e) {
      // 网络异常：退避重试
      if (attempt < MAX_RETRY) {
        log("  [飞书] 请求异常，退避后重试（" + attempt + "/" + MAX_RETRY + "）：" + String(e));
        Time.sleep(2000 * attempt);
        continue;
      }
      return { ok: false, reason: "重试 " + MAX_RETRY + " 次后仍异常：" + String(e).slice(0, 100) };
    }
  }
}

// ---------------------------------------------------------------- 表格读写（1.0 / 2.0 兼容层）
//
// 2.0（ET JSAPI）：Range.Value2 可读写；Range.Value 是方法（Value() 取值、
//                 Value(null, x) 赋值）；Worksheets.Add(Before, After, Count, Type)；
//                 Application.Enum 不存在。
// 1.0（databook）：Range.Value 可直接赋值；Sheets.Add(..., Enum.XlSheetType.xlWorksheet, 名字)。

function sheetsColl() {
  if (Application.Sheets) return Application.Sheets;
  if (Application.Worksheets) return Application.Worksheets;
  return null;
}

function getSheet(name) {
  var coll = sheetsColl();
  if (!coll) return null;
  try { return coll.Item(name); } catch (e1) {}
  try { return coll(name); } catch (e2) {} // 某些实现支持 Sheets('名字') 函数形式
  return null;
}

function activeSheetName() {
  try {
    if (Application.ActiveSheet && Application.ActiveSheet.Name) return Application.ActiveSheet.Name;
  } catch (e) {}
  return null;
}

function addSheet(name) {
  var coll = sheetsColl();
  if (!coll) return null;
  var after = activeSheetName();
  // databook 五参形式（名字随 Add 传入）
  try { return coll.Add(null, after, 1, XL_WORKSHEET, name); } catch (e1) {}
  // ET 四参形式：新建后改名
  var s = null;
  try { s = coll.Add(null, after, 1, XL_WORKSHEET); } catch (e2) {}
  if (s) {
    try { s.Name = name; return s; } catch (e3) { return s; }
  }
  return null;
}

/** 确保配置表存在并激活；失败则退回当前表（返回 false 时靠日志提示用户）。 */
function ensureSheet() {
  var s = getSheet(SHEET_NAME);
  if (!s) {
    s = addSheet(SHEET_NAME);
    if (!s) {
      log("  无法创建「" + SHEET_NAME + "」工作表，将直接使用当前表");
      return;
    }
    s = getSheet(SHEET_NAME) || s;
  }
  try { s.Activate(); } catch (e) {}
}

function rangeAt(addr) {
  try { return Application.Range(addr); } catch (e1) {}
  try { return Application.ActiveSheet.Range(addr); } catch (e2) {}
  return null;
}

/** 归一化单元格读到的值：过滤函数/对象等实现差异产物。 */
function cleanVal(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "function") return "";
  if (Array.isArray(v)) {
    var f = v[0];
    v = (f && typeof f === "object" && Array.isArray(f)) ? f[0] : f;
    if (v === null || v === undefined || typeof v === "object") return "";
  }
  if (typeof v === "object") return "";
  return String(v).replace(/^\s+|\s+$/g, "");
}

function readCell(addr) {
  var r = rangeAt(addr);
  if (!r) return "";
  var v;
  try { v = r.Value2; var a = cleanVal(v); if (a !== "") return a; } catch (e1) {}
  try { v = r.Value(); var b = cleanVal(v); if (b !== "") return b; } catch (e2) {}
  try { v = r.Text; return cleanVal(v); } catch (e3) {}
  return "";
}

function writeCell(addr, value) {
  var r = rangeAt(addr);
  if (!r) { log("  写入 " + addr + " 失败：取不到 Range"); return false; }
  try { r.Value2 = value; return true; } catch (e1) {}
  try { r.Value(null, value); return true; } catch (e2) {}
  try { r.Value = value; return true; } catch (e3) {} // 1.0 可写属性
  log("  写入 " + addr + " 失败：Value2 / Value 均不可写");
  return false;
}

/** 写入表头与默认值（幂等：已有内容的格子不动）。 */
function ensureLabels() {
  var labels = {
    A1: "WB_ACCESS_TOKEN", A2: "FEISHU_WEBHOOK", A3: "WB_ENDPOINT",
    A5: "签到结论", A6: "猫猫结论", A7: "整体", A8: "上次运行时间",
    A9: "飞书推送"
  };
  for (var addr in labels) {
    if (Object.prototype.hasOwnProperty.call(labels, addr) && readCell(addr) === "") {
      writeCell(addr, labels[addr]);
    }
  }
  if (readCell("B3") === "") {
    writeCell("B3", ENDPOINT_DEFAULT);
  }
}

function nowBeijing() {
  try {
    var d = new Date(Date.now() + 8 * 3600 * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
           " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds());
  } catch (e) {
    return "";
  }
}

// ---------------------------------------------------------------- 主流程

function main() {
  ensureSheet();
  ensureLabels();

  var token = readCell("B1");
  var webhook = readCell("B2");
  var endpoint = readCell("B3") || ENDPOINT_DEFAULT;

  if (!token) {
    log("缺少 token：请在「" + SHEET_NAME + "」表的 B1 粘贴 refresh_token.py 导出的凭证");
    writeCell("B5", "❌ 缺少 token（B1）");
    writeCell("B8", nowBeijing());
    return;
  }

  var headers = {
    "Accept": "application/json",
    "Authorization": "Bearer " + token,
    "Content-Type": "application/json",
    "User-Agent": "WorkBuddy"
  };

  log("=== 1/2 签到 ===");
  var checkinOk, checkin;
  try {
    var c = runCheckin(headers, endpoint);
    checkinOk = c[0];
    checkin = c[1];
  } catch (e) {
    checkinOk = false;
    checkin = { result: "ERROR", report: "签到模块异常：" + String(e) };
  }
  log("  结论：" + checkin.report);

  log("=== 2/2 猫猫旅行 ===");
  var buddy;
  try {
    buddy = runBuddy(headers, endpoint);
  } catch (e) {
    buddy = { result: "ERROR", report: "猫猫模块异常：" + String(e) };
  }
  log("  结论：" + (buddy.report || ""));

  // 整体成败只看签到：签到成功即成功
  var overallOk = checkinOk;
  var checkinLine = (checkinOk ? "✅ " : "❌ ") + checkin.report;
  var buddyLine = buddy.report ? "🐱 " + buddy.report : "🐱 无结论";

  // 推送结果也回写表格：业务结论（B5-B7）与推送状态（B9）分开，
  // 避免「表格显示成功但飞书没消息」时无从排查。
  // 推送失败的兜底：瞬时故障已自动重试；配置类失败改好 B2 后手动跑一次即可；
  // 若当天建了多个定时时间点，下一次运行会自动再推一遍当天结果。
  var pushLine;
  if (webhook) {
    var pr = pushFeishu(webhook, "每日自动化", checkinLine, buddyLine, overallOk);
    pushLine = pr.ok ? "✅ 已推送到飞书" : "❌ 推送失败：" + pr.reason;
  } else {
    log("  （B2 未填飞书 webhook，跳过推送）");
    pushLine = "未配置：B2 没填飞书地址，本次不推送";
  }

  // 结果回写表格
  writeCell("B5", checkinLine);
  writeCell("B6", buddyLine);
  writeCell("B7", overallOk ? "成功（签到已达成）" : "失败（签到未达成）");
  writeCell("B8", nowBeijing());
  writeCell("B9", pushLine);

  log("=== 完成：" + (overallOk ? "成功" : "失败") + " ===");
  log("  签到：" + checkinLine);
  log("  猫猫：" + buddyLine);
}

main();
