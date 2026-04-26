import os
import hmac
import sys
import json
import time
import ast
import signal
import hashlib
import pty
import re
import select
from urllib.request import Request, urlopen
from urllib.error import URLError
import config
DEEPSEEK_API_KEY=os.getenv("DEEPSEEK_API_KEY")
# ---------- 官方 Token 及模板 ----------
BOS = "<｜BOS｜>"
EOS = "<｜EOS｜>"
THINK_START = "<think>"
THINK_END = "</think>"
DSML = "|DSML|"
TC_BLOCK = "tool_calls"
SYS_TMPL = "{content}"
USER_TMPL = "{content}"
ASST_TMPL = "{reasoning}{content}{tool_calls}" + EOS
DSML_TMPL = "<{dsml}{block}>\n{invokes}\n</{dsml}{block}>"
INVOKE_TMPL = '<{dsml}invoke name="{name}">\n{args}\n</{dsml}invoke>'
PROMPT_PATTERNS = [
    rb"\$ ", rb"# ", rb">>> ", rb"\.\.\. ",
    rb"\[y/N\]", rb"\[Y/n\]", rb"\(yes/no\)",
    rb"[Pp]assword:", rb"[Pp]assword for ",
]
def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    elif obj is None:
        return None
    return obj
def encode_message(msg: dict) -> str:
    role = msg.get("role")
    content = msg.get("content", "") or ""
    if role == "system":
        return SYS_TMPL.format(content=content)
    elif role == "user":
        return USER_TMPL.format(content=content)
    elif role == "assistant":
        reasoning = msg.get("reasoning", "")
        tc_list = msg.get("tool_calls", [])
        invokes = []
        for tc in tc_list:
            func_name = tc.get("function", {}).get("name", "")
            func_args = tc.get("function", {}).get("arguments", "{}")
            if not isinstance(func_args, str):
                func_args = json.dumps(func_args, ensure_ascii=False)
            invokes.append(INVOKE_TMPL.format(dsml=DSML, name=func_name, args=func_args))
        tool_calls_block = ""
        if invokes:
            tool_calls_block = DSML_TMPL.format(dsml=DSML, block=TC_BLOCK, invokes="\n".join(invokes))
        reasoning_block = f"{THINK_START}\n{reasoning}\n{THINK_END}" if reasoning else ""
        return ASST_TMPL.format(reasoning=reasoning_block, content=content, tool_calls=tool_calls_block)
    elif role == "tool":
        return f"<tool_response>{content}</tool_response>"
    return ""
def encode_messages(messages: list) -> str:
    return BOS + "".join(encode_message(m) for m in messages)
SHELL_PID = None
SHELL_FD = None
def _start_shell():
    global SHELL_PID, SHELL_FD
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["PS1"] = "\\$ "
        os.environ["TERM"] = "dumb"
        os.execvp("bash", ["bash"])
    else:
        SHELL_PID = pid
        SHELL_FD = fd
        return _expect(PROMPT_PATTERNS, timeout=2) or b"session started"

def _proc_terminated():
    try:
        pid, status = os.waitpid(SHELL_PID, os.WNOHANG)
        return pid != 0
    except ChildProcessError:
        return True

def _expect(patterns, timeout=30):
    fd = SHELL_FD
    output = b""
    pattern_re = b"|".join(b"(?:" + p + b")" for p in patterns)
    deadline = time.time() + timeout if timeout else None
    while True:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            output += data
            if re.search(pattern_re, output, re.MULTILINE):
                return output
        if deadline and time.time() > deadline:
            return output + b"\n(timed out)"
    return output

def execute_terminal(command: str) -> str:
    global SHELL_FD, SHELL_PID
    if not SHELL_FD or SHELL_PID is None or _proc_terminated():
        _start_shell()

    if command.strip() == "^C":
        os.write(SHELL_FD, b"\x03")
        raw = _expect(PROMPT_PATTERNS, timeout=5)
        return raw.decode("utf-8", errors="replace").strip()

    if command.strip() in ("",):
        raw = _expect(PROMPT_PATTERNS, timeout=30)
        return raw.decode("utf-8", errors="replace").strip()

    if not config.DANGEROUS_ALLOW:
        print(f"\n[终端命令] {command}")
        choice = input("发送？(y=执行 / e=重写 / N=取消): ").strip().lower()
        if choice == 'y':
            pass
        elif choice == 'e':
            command = input("请输入要执行的命令: ")
            print(f"执行重写命令: {command}")
        else:
            return "用户取消"
    else:
        print(f"\n[自动执行] {command}")

    os.write(SHELL_FD, (command + "\n").encode())
    raw = _expect(PROMPT_PATTERNS, timeout=30)
    return raw.decode("utf-8", errors="replace").strip()

def handle_meta_compress(messages, args):
    if len(messages) < 3:
        return "错误：没有可压缩的 terminal 输出"
    prev = messages[-3]
    if prev.get("role") != "assistant" or "tool_calls" not in prev:
        return "错误：前一次is not tool call"
    if len(prev["tool_calls"])!=1:
        return "last call used more than one tools, can not compress"
    if prev["tool_calls"][0]["function"]["name"]!="terminal":
        return "last tool call is not terminal"
    messages[-2]["content"] = "[已压缩] 原始 terminal 输出已被模型压缩"
    return args["summary"]

# ---------- 健康检查 ----------
def health_check():
    print("正在进行健康检查...")
    if not DEEPSEEK_API_KEY:
        print("❌ 错误：未设置环境变量 DEEPSEEK_API_KEY")
        return False

    try:
        req = Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Accept": "application/json"}
        )
        with urlopen(req) as resp:
            balance_data = json.loads(resp.read().decode("utf-8"))
        if not balance_data.get("is_available", False):
            print("❌ 错误：API Key 无效或余额不足，请检查账户状态。")
            return False
        print("✅ API Key 有效，余额可用")
    except URLError as e:
        print(f"❌ 错误：无法连接到 DeepSeek API 进行验证 - {e}")
        return False
    except Exception as e:
        print(f"❌ 错误：验证 API Key 时发生未知错误 - {e}")
        return False

    if config.SKILLS:
        print("检查技能文件...")
        all_valid = True
        for path, desc in config.SKILLS.items():
            if not os.path.isfile(path):
                print(f"  ❌ 文件不存在: {path} (描述: {desc})")
                all_valid = False
            elif not os.access(path, os.R_OK):
                print(f"  ❌ 文件无读权限: {path} (描述: {desc})")
                all_valid = False
            else:
                print(f"  ✅ 可读: {path} - {desc}")
        if not all_valid:
            print("❌ 部分技能文件存在问题，请检查上述错误。")
            return False
    print("✅ 健康检查通过。")
    return True

# ---------- 会话加密 ----------
def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)

def _encrypt_messages(messages: list, password: str) -> bytes:
    plain = json.dumps(messages, ensure_ascii=False).encode('utf-8')
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    key_stream = hashlib.shake_256(key).digest(len(plain))
    ciphertext = bytes(a ^ b for a, b in zip(plain, key_stream))
    hmac = hashlib.sha256(key + ciphertext).digest()
    return salt + hmac + ciphertext

def _decrypt_messages(data: bytes, password: str) -> list:
    salt = data[:16]
    hmac_stored = data[16:48]
    ciphertext = data[48:]
    key = _derive_key(password, salt)
    expected_hmac = hashlib.sha256(key + ciphertext).digest()
    if not hmac.compare_digest(expected_hmac, hmac_stored):
        raise ValueError("密码错误或文件已损坏")
    key_stream = hashlib.shake_256(key).digest(len(ciphertext))
    plain = bytes(a ^ b for a, b in zip(ciphertext, key_stream))
    return json.loads(plain.decode('utf-8'))

def stream_chat(messages: list):
    clean = _sanitize(messages)
    payload = json.dumps({
        "model": config.MODEL,
        "messages": clean,
        "stream": config.STREAM,
        "tools": config.TOOLS,
        "temperature": config.TEMPERATURE,
        "top_p": config.TOP_P,
        "frequency_penalty": config.FREQUENCY_PENALTY,
        "presence_penalty": config.PRESENCE_PENALTY,
        "max_tokens": config.MAX_TOKENS,
        **({"thinking": {"type": "enabled", "reasoning_effort": config.REASONING_EFFORT}} if config.REASONING_EFFORT else {})
    }).encode("utf-8")
    # print("\n[DEBUG]",payload)
    req = Request("https://api.deepseek.com/chat/completions", data=payload, headers={
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    })

    try:
        with urlopen(req) as resp:
            assistant_msg = {"role": "assistant", "content": "", "reasoning": "", "tool_calls": []}
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                    if "reasoning_content" in delta and delta["reasoning_content"] is not None:
                        print(delta["reasoning_content"], end="", flush=True)
                        assistant_msg["reasoning"] += delta["reasoning_content"]
                    if "content" in delta and delta["content"] is not None:
                        print(delta["content"], end="", flush=True)
                        assistant_msg["content"] += delta["content"]


                    if "tool_calls" in delta:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            while len(assistant_msg["tool_calls"]) <= idx:
                                unique_id=f"call_{idx}"
                                assistant_msg["tool_calls"].append({
                                    "id": unique_id,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                })
                            func=tc.get("function")
                            if isinstance(func,dict):
                                if "name" in func:
                                    assistant_msg["tool_calls"][idx]["function"]["name"] += tc["function"]["name"]
                                if "arguments" in func:
                                    assistant_msg["tool_calls"][idx]["function"]["arguments"] += tc["function"]["arguments"]
                except Exception:
                    pass
        print()
        # for tc in assistant_msg["tool_calls"]:
        #     if isinstance(tc["function"]["arguments"],str):
        #         try:
        #             tc["function"]["arguments"]=json.loads(tc["function"]["arguments"])
        #         except:
        #             pass
        return assistant_msg
    except KeyboardInterrupt:
        print("\n[生成已中断]", flush=True)
        return None

    except URLError as e:
        print(f"请求失败: {e}")
        if hasattr(e, 'read'):
            print(e.read().decode())
        return None

    # except URLError as e:
    #     print(f"请求失败: {e}")
    #     return None

if not health_check():
    sys.exit(1)

messages = []
print(f"DeepSeek-V4 CLI (模型={config.MODEL}, 推理={config.REASONING_EFFORT}) 已启动。")
print("特殊命令：/save /load /health /parameter_show /config_show /parameter_save /exit")
print("动态修改配置： :VAR VALUE (如 :DANGEROUS_ALLOW True)")
print("本地执行：!<命令> (如 !ls -la)")

while True:
    try:
        user_input = input("\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n再见！")
        break

    if not user_input:
        continue

    if user_input.startswith("/"):
        parts = user_input.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd == "/exit":
            break

        elif cmd == "/save":
            if len(parts) < 2:
                print("用法: /save <文件名>")
                continue
            filename = parts[1]
            import getpass
            pw = getpass.getpass("请输入加密密码 (至少 8 位): ")
            if len(pw) < 8:
                print("密码长度必须至少 8 位")
                continue
            try:
                encrypted = _encrypt_messages(messages, pw)
                with open(filename, 'wb') as f:
                    f.write(encrypted)
                print(f"加密会话已保存到 {filename}")
            except Exception as e:
                print(f"保存失败: {e}")
            continue

        elif cmd == "/load":
            if len(parts) < 2:
                print("用法: /load <文件名>")
                continue
            filename = parts[1]
            import getpass
            pw = getpass.getpass("请输入解密密码: ")
            try:
                with open(filename, 'rb') as f:
                    data = f.read()
                messages = _decrypt_messages(data, pw)
                print(f"已加载加密会话 {filename}")
                # 提示环境变更
                messages.append({
                    "role": "system",
                    "content": (
                        "注意：当前终端环境已被重新初始化，之前的 shell 进程已退出。"
                        "如果之前有未完成的终端操作，请使用 `terminal` 工具重新启动必要的程序。"
                    )
                })
                if SHELL_PID:
                    try:
                        os.kill(SHELL_PID, signal.SIGKILL)
                        os.waitpid(SHELL_PID, 0)
                    except:
                        pass
                    SHELL_PID = None
                    SHELL_FD = None
            except FileNotFoundError:
                print(f"文件不存在: {filename}")
            except ValueError:
                print("密码错误或文件已损坏")
            except Exception as e:
                print(f"加载失败: {e}")
            continue

        elif cmd == "/health":
            health_check()
            continue

        elif cmd == "/parameter_show":
            for k, v in vars(config).items():
                if not k.startswith("_") and not isinstance(v, type(os)):
                    print(f"  {k} = {repr(v)}")
            continue

        elif cmd == "/config_show":
            cfg_path = getattr(config, "__file__", None)
            if not cfg_path:
                print("无法找到 config.py 路径")
            else:
                try:
                    with open(cfg_path, "r") as f:
                        print(f.read())
                except Exception as e:
                    print(f"读取失败: {e}")
            continue

        elif cmd == "/parameter_save":
            cfg_path = getattr(config, "__file__", None)
            if not cfg_path:
                print("无法找到 config.py 路径")
                continue
            print("警告：将用当前内存值覆盖 config.py，所有注释将丢失。")
            confirm = input("确认？(y/N): ").strip().lower()
            if confirm != "y":
                print("已取消")
                continue
            try:
                lines = [""]
                for k, v in vars(config).items():
                    if k.startswith("_") or isinstance(v, type(os)):
                        continue
                    lines.append(f"{k} = {repr(v)}\n")
                with open(cfg_path, "w") as f:
                    f.writelines(lines)
                print("配置已保存到", cfg_path)
            except Exception as e:
                print(f"保存失败: {e}")
            continue


        else:
            print("未知命令。可用: /save /load /health /parameter_show /config_show /parameter_save /exit")
            continue

    # 动态配置修改命令
    elif user_input.startswith(":"):
        parts = user_input[1:].strip().split(maxsplit=1)
        if len(parts) != 2:
            print("用法：: <变量名> <值>")
            continue
        var, val_str = parts[0].strip(), parts[1].strip()
        if not hasattr(config, var):
            print(f"未知变量：{var}")
            continue
        try:
            new_val = ast.literal_eval(val_str)
        except (ValueError, SyntaxError):
            print("值格式错误，请输入合法的 Python 字面量")
            continue
        setattr(config, var, new_val)
        print(f"已设置 {var} = {repr(new_val)}")
        continue

    # 本地命令直通终端
    elif user_input.startswith("!"):
        command = user_input[1:].strip()
        if not command:
            print("用法：! <命令>")
            continue
        fake_id = f"user_direct_{int(time.time())}"
        messages.append({
            "role": "assistant",
            "reasoning_content":"",
            "content": "",
            "tool_calls": [{
                "id": fake_id,
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments":json.dumps( {"command": command},ensure_ascii=False)
                }
            }]
        })
        result = execute_terminal(command)
        print(f"[结果]\n{result[:1000]}")
        messages.append({
            "role": "tool",
            "content":result,
            "tool_call_id":fake_id
        })
        continue

    # 正常对话
    messages.append({"role": "user", "content": user_input})
    resp = stream_chat(messages)
    if resp is None:
        print("最近对话：")
        for m in messages[-3:]:
            role = m.get("role", "?")
            content = m.get("content", "")
            print(f"  [{role}] {content[:200]}")
        continue
    # assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
    assistant_msg = {"role": "assistant", "content": resp.get("content") or "", "reasoning_content":""}
    if resp.get("tool_calls"):
        assistant_msg["tool_calls"] = resp["tool_calls"]
    messages.append(assistant_msg)
    while resp and resp.get("tool_calls"):
        for tc in resp["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            if name == "terminal":
                result = execute_terminal(args.get("command", ""))
            elif name == "meta_compress":
                result = handle_meta_compress(messages, args)
            else:
                result = f"未知工具: {name}"
            print(f"[工具结果]\n{result[:1000]}")
            # messages.append({
            #     "role": "user",
            #     "content":f"<tool_result>{result}</tool_result>" ,
            # })
            messages.append({
                "role": "tool",
                "content":result,
                "tool_call_id":tc.get("id","")
            })

        # 让模型继续生成
        resp = stream_chat(messages)
        if resp is None:
            print("\n[模型中断或出错]")
            break
        # assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
        # assistant_msg = {"role": "assistant", "content": resp.get("content") or ""}
        assistant_msg = {"role": "assistant", "content": resp.get("content") or "", "reasoning_content":""}
        if resp.get("tool_calls"):
            assistant_msg["tool_calls"] = resp["tool_calls"]
        messages.append(assistant_msg)


# 清理子进程
if SHELL_PID:
    try:
        os.kill(SHELL_PID, signal.SIGKILL)
        os.waitpid(SHELL_PID, 0)
    except:
        pass
