import os

ENV_PATH = os.path.join(os.getcwd(), ".env")

def set_env_value(key: str, value: str) -> None:
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()

    out = []
    found = False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)

    if not found:
        out.append(f"{key}={value}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
