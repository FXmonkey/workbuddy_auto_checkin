#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refresh_token.py —— 在你自己的电脑上运行，把 WorkBuddy 登录态导出成 GitHub Secret。

云端 runner 没有本机登录态，也无法解密 Electron safeStorage 保护的凭据；
因此 token 必须由本机脚本导出，再写入仓库 Secret（绝不进仓库、绝不进日志）。

用法：
    python refresh_token.py                       # 打印 token + 手动配置指引
    python refresh_token.py --gh-repo OWNER/REPO  # 有 gh CLI 时直接写入 Secret
                                                  # 没有 gh 则打印 token + 手动配置指引
（打包成 refresh_token.exe 后用法相同，双击运行等价于无参数形式）

安全约定：
  - 明文 token 只出现在本进程内存与最终那一次目标位置；
  - 命令行参数、环境变量、日志均不含明文 token；
  - 不写临时 token 文件。
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time

# 助手调用的总时限（秒）。客户端体积与系统负载会让 Electron 冷启动变慢，
# 实测冷启动可到 50~80 秒，因此不再用「30 秒一刀切」判定失败。
AUTH_TIMEOUT = 120.0
# 首次等待的片长（秒）：超时后不杀进程，按倍数延长等待同一进程。
# 冷启动的代价已经付过，重启反而要重新付一次，所以宁可继续等。
AUTH_TIMEOUT_SLICE = 45.0
AUTH_TIMEOUT_STEP = 2.0
INPUT_LIMIT = 65536
OUTPUT_LIMIT = 65536
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")

# 固定代码只出现在 argv；加密信封走 stdin，构建材料不出本进程。
# 仅支持 sym-v1 / field / suite 1。
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const token = utf8(plaintext);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      failure('INVALID_FORMAT');
    return token;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, accessToken: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


def find_auth_file():
    """按平台探测凭据文件；WORKBUDDY_AUTH_FILE 可覆盖。"""
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        return override if os.path.exists(override) else None
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [
        os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
        os.path.join(local, "workbuddy-desktop.info"),
        os.path.join(home, "Library", "Application Support", "workbuddy-desktop.info"),
        os.path.join(home, ".config", "workbuddy-desktop.info"),
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def find_runtime():
    """定位 WorkBuddy 可执行文件：环境变量 → 常见安装目录 → 注册表。"""
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        if os.path.isfile(override):
            return os.path.abspath(override)
        raise SystemExit("WORKBUDDY_EXE 指向的文件不存在")
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe")]
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(name):
            candidates.append(os.path.join(os.environ[name], "WorkBuddy", "WorkBuddy.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    if sys.platform == "win32":
        found = _registry_runtime()
        if found:
            return found
    # 兜底：常见非默认安装位置（含本机实测的 D:\WorkBuddy）
    for root in ("C:/", "D:/", "E:/"):
        p = os.path.join(root, "WorkBuddy", "WorkBuddy.exe")
        if os.path.isfile(p):
            return os.path.abspath(p)
    raise SystemExit("未找到 WorkBuddy 客户端，请设置 WORKBUDDY_EXE 环境变量")


def _registry_runtime():
    """只读取注册表字符串值，绝不解释卸载命令。"""
    try:
        import winreg
    except ImportError:
        return None
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as entry:
                                name, _ = winreg.QueryValueEx(entry, "DisplayName")
                                if not isinstance(name, str) or "workbuddy" not in name.lower():
                                    continue
                                icon, _ = winreg.QueryValueEx(entry, "DisplayIcon")
                                if isinstance(icon, str):
                                    path = icon.split(",")[0].strip().strip('"')
                                    if os.path.isfile(path):
                                        return os.path.abspath(path)
                        except OSError:
                            continue
            except OSError:
                continue
    return None


def run_helper(exe, request):
    """所有管道与超时都有界；异常中绝不含捕获的数据。

    超时策略：不因一次等待超时就杀掉助手进程。Electron 冷启动可能要几十秒，
    杀掉重启等于把已经付过的冷启动代价重来一遍。因此首轮超时后改为
    「继续等同一个进程、每轮把等待上限翻倍」，直到用满总时限 AUTH_TIMEOUT。
    """
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    proc = subprocess.Popen(
        [exe, "-e", AUTH_HELPER_JS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + AUTH_TIMEOUT
    wait = min(AUTH_TIMEOUT_SLICE, AUTH_TIMEOUT)
    sent = False
    while True:
        try:
            if sent:
                # 已写过 stdin，续读时必须传 None，否则 communicate 会拒绝重发。
                out, err = proc.communicate(timeout=wait)
            else:
                # 先置位再调用：即使这一轮超时抛异常，下一轮也只能续读、不能重发。
                sent = True
                out, err = proc.communicate(data, timeout=wait)
            break
        except subprocess.TimeoutExpired:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.communicate()
                raise SystemExit("解密助手超时")
            # 进程还在跑，继续等它；每轮等待上限翻倍，但不超过剩余总时限。
            wait = max(min(wait * AUTH_TIMEOUT_STEP, remaining), 0.5)
            sys.stderr.write("解密助手仍在启动，继续等待（总时限剩余 %.0f 秒）…\n" % remaining)
    if len(out) > OUTPUT_LIMIT:
        raise SystemExit("解密助手输出超限")
    try:
        reply = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise SystemExit("解密助手返回无效结果")
    if reply.get("ok") is not True:
        raise SystemExit("解密失败：" + str(reply.get("reason", "UNKNOWN")))
    return reply


def load_session(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_repo(value):
    """接受 OWNER/REPO 或 .git 后缀形式，统一成 OWNER/REPO 供 gh 与 URL 使用。"""
    repo = value.strip().rstrip("/")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise SystemExit("仓库格式应为 OWNER/REPO（例如 FXmonkey/workbuddy_auto），收到：%s" % value)
    return repo


def main():
    gh_repo = None
    args = sys.argv[1:]
    if "--gh-repo" in args:
        gh_repo = normalize_repo(args[args.index("--gh-repo") + 1])

    auth_file = find_auth_file()
    if not auth_file:
        raise SystemExit("未找到凭据文件，请设置 WORKBUDDY_AUTH_FILE")
    session = load_session(auth_file)
    token = session.get("auth", {}).get("accessToken")
    if not token:
        raise SystemExit("凭据文件缺少 accessToken，请先在 WorkBuddy 登录")

    if isinstance(token, str):
        print(token)
    else:
        exe = find_runtime()
        sys.stderr.write("凭据为加密信封，正在通过本机客户端解密…\n")
        token = run_helper(exe, {"operation": "decrypt", "value": token})["accessToken"]

    if not (isinstance(token, str) and TOKEN_RE.fullmatch(token)):
        raise SystemExit("token 格式异常")

    # 有 gh 且指定了仓库 → 自动写入；否则统一走「指引 + 起止标记 + token」，
    # 保证无论是终端跑 .py 还是双击 exe，输出结构一致、都能照着做
    if gh_repo and shutil.which("gh") is not None:
        # 明文 token 只作为 stdin 传给 gh，不出现在命令行与进程列表中
        proc = subprocess.run(
            ["gh", "secret", "set", "WB_ACCESS_TOKEN", "--repo", gh_repo, "--body", "-"],
            input=token.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SystemExit("写入 GitHub Secret 失败：" + proc.stderr.decode("utf-8", "replace"))
        print("已写入 GitHub Secret WB_ACCESS_TOKEN（仓库 %s）" % gh_repo)
        return

    if gh_repo:
        print("\n[提示] 未检测到 GitHub CLI（gh），无法自动写入 Secret。")
    print("\n请手动配置：")
    print("  1) 复制下方两个标记之间的 token")
    if gh_repo:
        print("  2) 打开 https://github.com/%s/settings/secrets/actions/new" % gh_repo)
        print("  3) Name 填 WB_ACCESS_TOKEN，Secret 粘贴 token，保存")
        print("如需自动写入，安装 gh CLI（https://cli.github.com/）后执行 gh auth login")
    else:
        print("  2) GitHub Actions 版：你仓库的 Settings → Secrets and variables → Actions，"
             "新建 WB_ACCESS_TOKEN 并粘贴")
        print("     WPS 金山文档版：「签到自动化」表的 B1 单元格粘贴")
    print("\n----- 以下为 token -----")
    print(token)
    print("----- 以上为 token -----")
    print("\n[安全提醒] token 等同登录态，请勿粘贴到聊天、日志或任何第三方站点。", file=sys.stderr)


def _pause_if_frozen():
    """打包成 exe 被双击运行时，结束前停一下，让用户来得及复制输出。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        input("\n按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        _pause_if_frozen()