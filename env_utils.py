import os

ENV_PATH = os.path.join(os.getcwd(), ".env")

def set_env_value(key: str, value: str) -> None:
    """
    Upserts KEY=VALUE into .env safely.
    """
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
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

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(out) + "\n")


def get_env_value(key: str, default: str = "") -> str:
    """
    Reads KEY from .env (not from process env). Useful for runtime admin updates.
    """
    if not os.path.exists(ENV_PATH):
        return default
    with open(ENV_PATH, "r") as f:
        for ln in f.read().splitlines():
            if ln.strip().startswith(f"{key}="):
                return ln.split("=", 1)[1].strip()
    return default
