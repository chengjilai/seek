import json
import pathlib
import os
import difflib
from openai import OpenAI

client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
tools = [{
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "write or append to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "filename, relative or absolute"},
                "content": {"type": "string", "description": "content"},
                "mode": {"type": "string", "enum": ["write", "append"], "description": "'write' create and write and overwrites if it exists, 'append' adds to end."}
            },
            "required": ["path", "content"],
            "additionalProperties": False
        }
    }
}]
def write_file(path: str, content: str, mode: str = "write") -> str:
    try:
        m = "w" if mode == "write" else "a"
        with open(path, m, encoding="utf-8") as f:
            f.write(content)
        return f"{'wrote to' if m == 'w' else 'appended to'} {path}"
    except Exception as e:
        return f"Error: {e}"
AVAILABLE_FUNCTIONS = {"write_file": write_file}

messages = []
print("type '/exit' to quit.\n")
while True:
    user_input = input("You:\n").strip()
    if user_input == "/exit":
        break
    messages.append({"role": "user", "content": user_input})
    while True:
        resp = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=messages,
            tools=tools,
        )
        msg = resp.choices[0].message
        finish_reason = resp.choices[0].finish_reason
        messages.append(msg)
        if finish_reason == "tool_calls":
            if msg.content:
                print(f"Model:\n{msg.content}")
            for call in msg.tool_calls:
                name = call.function.name
                args = json.loads(call.function.arguments)
                if name == "write_file":
                    old=""
                    if pathlib.Path(args["path"]).exists():
                        with open(args["path"], "r", encoding="utf-8") as f:
                            old = f.read()
                    new = old + args["content"] if args.get("mode") == "append" else args["content"]
                    diff = difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        fromfile=f"{args['path']} (current)",
                        tofile=f"{args['path']} (after change)"
                    )
                    print()
                    print("".join(diff).strip())
                print(f"\nmodel wants: {name}({args})")
                if input("\ntype 'y' to approve ").strip().lower() == "y":
                    result = AVAILABLE_FUNCTIONS[name](**args)
                    print(f"\n{result}")
                else:
                    result = "user denied tool execution."
                    print("rejected.")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
            continue
        elif finish_reason == "stop":
            print(f"\nModel:\n{msg.content}")
            break
        else:
            print(f"unexpected finish: {finish_reason}")
            break
