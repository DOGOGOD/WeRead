"""
WeRead - 微信公众号文章爬取与AI摘要（OpenClaw Skill）

用法:
  python main.py qrcode       # 生成登录二维码
  python main.py login         # 生成二维码 + 等待扫码登录
  python main.py once          # 立即爬取一次，输出结构化文章数据
  python main.py start         # 启动定时调度（默认每天 10:00, 14:00, 18:00, 22:00）
  python main.py test          # 检查配置和连接状态

OpenClaw 集成流程:
  python main.py login (后台) → 读取 data/wx_qrcode.png → 发送给用户扫码
  python main.py once  → 读取 data/articles/*.md → AI 总结 → 发送用户
  python main.py start → 定时自动执行
"""
import asyncio
import os
import sys
import yaml
import argparse

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from seek.models import Subscription, TokenStore
from seek.scheduler import WeReadScheduler
from seek.auth import WeChatAuth


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        print(f"错误: 配置文件 {path} 不存在")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_subscriptions(cfg: dict) -> list[Subscription]:
    token_store = TokenStore()
    stored_data = token_store.load()
    stored_subs = stored_data.get("subscriptions", [])

    subs = []
    raw_subs = cfg.get("subscriptions") or []
    for item in raw_subs:
        name = item.get("name", "")
        url = item.get("url", "")
        if not url:
            print(f"警告: 订阅项缺少 url，跳过 ({name})")
            continue

        existing = None
        for s in stored_subs:
            if s.get("url") == url:
                existing = s
                break

        sub = Subscription(
            name=name or (existing.get("name") if existing else ""),
            url=url,
            keywords=item.get("keywords", []),
            interest=item.get("interest", ""),
            fakeid=existing.get("fakeid", "") if existing else "",
            biz=existing.get("biz", "") if existing else "",
            mp_id=existing.get("mp_id", "") if existing else "",
        )
        subs.append(sub)

    return subs


async def _init_fakeids(auth: WeChatAuth, subs: list[Subscription]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.webkit.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        for sub in subs:
            if sub.fakeid:
                print(f"  [{sub.name or sub.url[:30]}] 已有 fakeid，跳过")
                continue
            fakeid = await auth.get_fakeid_by_article_url(page, sub.url)
            if fakeid:
                sub.fakeid = fakeid
                print(f"  [{sub.name or sub.url[:30]}] fakeid={fakeid}")
            else:
                print(f"  [{sub.name or sub.url[:30]}] 提取 fakeid 失败")

        await browser.close()

    data = TokenStore().load()
    data["subscriptions"] = [s.to_dict() for s in subs]
    TokenStore().save(data)


def cmd_qrcode(cfg: dict) -> None:
    """仅生成登录二维码"""
    print("=" * 60)
    print("WeRead - 生成登录二维码")
    print("=" * 60)

    auth = WeChatAuth()
    path = auth.generate_qrcode()
    if path:
        print(f"QRCODE_PATH:{path}")
        print("\n请将此二维码发送给用户，让用户使用微信扫码。")
        print("用户扫码后，运行 'python main.py login' 完成登录。")
    else:
        print("ERROR: 二维码生成失败")
        sys.exit(1)


def cmd_login(cfg: dict) -> None:
    """登录 + 初始化订阅 fakeid"""
    print("=" * 60)
    print("WeRead - 微信公众平台登录")
    print("=" * 60)

    auth = WeChatAuth()
    if not auth.login_with_qrcode_and_wait(timeout_seconds=300):
        print("登录失败，请重试")
        sys.exit(1)

    subs = load_subscriptions(cfg)
    if not subs:
        print("提示: 配置文件中没有订阅")
        return

    print("\n正在初始化各公众号信息...")
    asyncio.run(_init_fakeids(auth, subs))

    print("\n登录初始化完成。")


def cmd_once(cfg: dict) -> None:
    """爬取一次，输出结构化文章数据到 Markdown"""
    subs = load_subscriptions(cfg)
    if not subs:
        print("错误: 没有已配置的订阅")
        sys.exit(1)

    token_store = TokenStore()
    if not token_store.get_token():
        print("错误: 未登录，请先运行: python main.py login")
        sys.exit(1)

    for sub in subs:
        if not sub.fakeid:
            print(f"警告: [{sub.name}] 缺少 fakeid，可能无法获取文章列表")

    schedule_cfg = cfg.get("schedule", {})

    scheduler = WeReadScheduler(
        subscriptions=subs,
        token_store=token_store,
        max_pages=schedule_cfg.get("max_pages", 2),
        request_interval=schedule_cfg.get("request_interval", 10),
    )

    filepath = scheduler.run_once()

    if filepath:
        print(f"\n爬取完成。请 OpenClaw AI 读取 {filepath} 进行总结。")
    else:
        print("\n本期时间窗口内无新文章。")


def cmd_start(cfg: dict) -> None:
    """启动定时调度"""
    subs = load_subscriptions(cfg)
    if not subs:
        print("错误: 没有已配置的订阅")
        sys.exit(1)

    token_store = TokenStore()
    if not token_store.get_token():
        print("错误: 未登录，请先运行: python main.py login")
        sys.exit(1)

    schedule_cfg = cfg.get("schedule", {})

    scheduler = WeReadScheduler(
        subscriptions=subs,
        token_store=token_store,
        max_pages=schedule_cfg.get("max_pages", 2),
        request_interval=schedule_cfg.get("request_interval", 10),
    )

    cron_exprs = schedule_cfg.get("cron_expressions", [])
    scheduler.start(cron_exprs)

    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止...")
        scheduler.stop()


def cmd_test(cfg: dict) -> None:
    """检查配置状态"""
    print("WeRead - 状态检查")
    print("=" * 40)

    subs = load_subscriptions(cfg)
    print(f"订阅数量: {len(subs)}")
    for s in subs:
        print(f"  - {s.name or '(未命名)'}: fakeid={'已设置' if s.fakeid else '(无)'}")

    token_store = TokenStore()
    token = token_store.get_token()
    print(f"微信认证: {'已登录' if token else '未登录'}")

    print(f"AI 总结: 由 OpenClaw 内置 AI 完成（无需配置外部 API）")


def main():
    parser = argparse.ArgumentParser(description="WeRead - 微信公众号文章爬取与AI摘要")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=["qrcode", "login", "once", "start", "test"],
        help="qrcode(生成二维码) | login(登录) | once(爬取) | start(定时) | test(检查)",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()
    cfg = load_config(args.config)

    commands = {
        "qrcode": cmd_qrcode,
        "login": cmd_login,
        "once": cmd_once,
        "start": cmd_start,
        "test": cmd_test,
    }
    commands[args.command](cfg)


if __name__ == "__main__":
    main()
