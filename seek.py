import os
import termios
import hmac
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
if not DEEPSEEK_API_KEY:
    print("environment variable DEEPSEEK_API_KEY not set")
    exit()
PROMPT_PATTERNS = [
    rb"^\$ ", rb"^# ", rb">>> ", rb"\.\.\. ",
    rb"\[y/N\]", rb"\[Y/n\]", rb"\(yes/no\)",
    rb"[Pp]assword:", rb"[Pp]assword for ",
    rb"^> ",rb"\[y/N/e\]"
]
def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj
SHELL_PID = None
SHELL_FD = None
def _start_shell():
    global SHELL_PID, SHELL_FD
    pid, fd = pty.fork()
    if pid == 0:
        attrs = termios.tcgetattr(0)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)
        os.environ["PS1"] = "\\$ "
        os.environ["PS2"] = ""
        os.environ["TERM"] = "dumb"
        os.environ["PAGER"] = "cat"
        os.execvp("bash", ["bash","--norc"])
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
    global SHELL_FD, SHELL_PID
    fd = SHELL_FD
    output = b""
    pattern_re = b"|".join(b"(?:" + p + b")" for p in patterns)
    deadline = time.time() + timeout if timeout else None
    while True:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            output += data
            if re.search(pattern_re, output, re.MULTILINE):
                drain_deadline = time.time() + 0.1
                while time.time() < drain_deadline:
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if not r:
                        break
                    try:
                        extra = os.read(fd, 65536)
                    except OSError:
                        break
                    if not extra:
                        break
                    output += extra
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
        print(f"\n{command}")
        choice = input("[y/N/e] ").strip().lower()
        if choice=='y':
            pass
        elif choice == 'e':
            command = input("command:\n")
        else:
            return "user denied"
    else:
        print(f"\n{command}")
    os.write(SHELL_FD, (command + "\n").encode())
    raw = _expect(PROMPT_PATTERNS, timeout=30)
    return raw.decode("utf-8", errors="replace").strip()
def handle_meta_compress(messages, args):
    if len(messages) < 3:
        return "no terminal output can be compressed"
    prev = messages[-3]
    if prev.get("role") != "assistant" or "tool_calls" not in prev:
        return "last message is not tool call"
    if len(prev["tool_calls"])!=1:
        return "last call used more than one tools, can not compress"
    if prev["tool_calls"][0]["function"]["name"]!="terminal":
        return "last tool call is not terminal"
    messages[-2]["content"] = "[original output is compressed]"
    return args["summary"]
def health():
    if not DEEPSEEK_API_KEY:
        print("DEEPSEEK_API_KEY environment variable not found")
        return
    try:
        req = Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Accept": "application/json"}
        )
        with urlopen(req) as resp:
            balance_data = json.loads(resp.read().decode("utf-8"))
        if not balance_data.get("is_available", False):
            print("balance not available")
            return
    except URLError as e:
        print(f"failed to connect to deepseek endpoint- {e}")
        return
    except Exception as e:
        print(f"{e}")
        return
    if config.SKILLS:
        all_valid = True
        for path, desc in config.SKILLS.items():
            if not os.path.isfile(path):
                print(f"no such file: {path}")
                all_valid = False
            elif not os.access(path, os.R_OK):
                print(f"no read access to: {path}")
                all_valid = False
        if not all_valid:
            return
    return

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
        return assistant_msg
    except KeyboardInterrupt:
        print("\n[生成已中断]", flush=True)
        return None

    except URLError as e:
        print(f"请求失败: {e}")
        if hasattr(e, 'read'):
            print(e.read().decode())
        return None

health()
print(f"MODEL: {config.MODEL}\nREASONING_EFFORT: {config.REASONING_EFFORT}\nFREQUENCY_PENALTY: {config.FREQUENCY_PENALTY}\nMAX_TOKEN: {config.MAX_TOKENS}\nTEMPERATURE: {config.TEMPERATURE}\nTOP_P: {config.TOP_P}\nDANGEROUS_ALLOW{config.DANGEROUS_ALLOW}\nSTREAM: {config.STREAM}\nSKILLS: {config.SKILLS}")
print("/save /load /health /parameter_show /config_show /parameter_save /exit")
print(":parameter value")
print("!command")
messages = []
messages.append({
    "role": "system",
    "content": (
        f"check these files when needed\n{config.SKILLS}"
    )
})
while True:
    user_input = input("\n> ").strip()
    if not user_input:
        continue
    if user_input.startswith("/"):
        parts = user_input.split()
        cmd = parts[0]
        if cmd == "/exit":
            break
        elif cmd == "/save":
            import getpass
            pw = getpass.getpass("请输入加密密码 (至少 8 位): ")
            encrypted = _encrypt_messages(messages, pw)
            with open(parts[1], 'wb') as f:
                f.write(encrypted)
            continue
        elif cmd == "/load":
            import getpass
            pw = getpass.getpass("请输入密码: ")
            try:
                with open(parts[1], 'rb') as f:
                    data = f.read()
                messages = _decrypt_messages(data, pw)
                messages.append({
                    "role": "system",
                    "content": (
                        "注意：当前终端环境已被重新初始化，之前的 shell 进程已退出。"
                    )
                })
                if SHELL_PID:
                    os.kill(SHELL_PID, signal.SIGKILL)
                    os.waitpid(SHELL_PID, 0)
                    SHELL_PID = None
                    SHELL_FD = None
            except Exception as e:
                print(f"加载失败: {e}")
            continue
        elif cmd == "/health":
            health()
            continue
        elif cmd == "/parameter_show":
            print(f"MODEL: {config.MODEL}\nREASONING_EFFORT: {config.REASONING_EFFORT}\nFREQUENCY_PENALTY: {config.FREQUENCY_PENALTY}\nMAX_TOKEN: {config.MAX_TOKENS}\nTEMPERATURE: {config.TEMPERATURE}\nTOP_P: {config.TOP_P}\nDANGEROUS_ALLOW{config.DANGEROUS_ALLOW}\nSTREAM: {config.STREAM}\nSKILLS: {config.SKILLS}")
            continue
        elif cmd == "/config_show":
            cfg_path = getattr(config, "__file__")
            with open(cfg_path, "r") as f:
                print(f.read())
            continue
        elif cmd == "/parameter_save":
            cfg_path = getattr(config, "__file__")
            confirm = input("(y/N): ").strip().lower()
            if confirm != "y":
                continue
            lines = [""]
            for k, v in vars(config).items():
                if k.startswith("_") or isinstance(v, type(os)):
                    continue
                lines.append(f"{k} = {repr(v)}\n")
            with open(cfg_path, "w") as f:
                f.writelines(lines)
                print("配置已保存到", cfg_path)
            continue
        else:
            continue
    elif user_input.startswith(":"):
        parts = user_input[1:].strip().split()
        var, val_str = parts[0].strip(), parts[1].strip()
        new_val = ast.literal_eval(val_str)
        setattr(config, var, new_val)
        continue
    elif user_input.startswith("!"):
        command = user_input[1:].strip()
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
        print(f"\n{result}")
        messages.append({
            "role": "tool",
            "content":result,
            "tool_call_id":fake_id
        })
        continue
    messages.append({"role": "user", "content": user_input})
    resp = stream_chat(messages)
    assistant_msg = {"role": "assistant", "content": resp.get("content") or "", "reasoning_content":""}
    if resp.get("tool_calls"):
        assistant_msg["tool_calls"] = resp["tool_calls"]
    messages.append(assistant_msg)
    while resp.get("tool_calls"):
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
                result = f"unknown tool: {name}"
            print(f"\n{result}")
            messages.append({
                "role": "tool",
                "content":result,
                "tool_call_id":tc.get("id","")
            })
        resp = stream_chat(messages)
        assistant_msg = {"role": "assistant", "content": resp.get("content") or "", "reasoning_content":""}
        if resp.get("tool_calls"):
            assistant_msg["tool_calls"] = resp["tool_calls"]
        messages.append(assistant_msg)

if SHELL_PID:
    os.kill(SHELL_PID, signal.SIGKILL)
    os.waitpid(SHELL_PID, 0)
