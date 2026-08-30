"""Presentation helpers shared by Telegram and backup progress messages."""


def render_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "█" * filled + "░" * (width - filled)


def position_token(n: int) -> str:
    if 1 <= n <= 9:
        return "①②③④⑤⑥⑦⑧⑨"[n - 1]
    return str(n)
