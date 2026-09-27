#!/usr/bin/env python3
"""Banner of Truth (UK) の商品価格を取得し、しきい値を下回ったら通知フラグを立てる。

- 価格は WooCommerce の Store API（/wp-json/wc/store/v1/products）から取得する
  （HTML をスクリプトで解析するよりテーマ変更に強い）。
- 商品ページ URL の slug から親商品を引き、指定した装丁（BINDING）の variation を
  探して、その価格を見る。variation ID を固定しないので、作り直されても追従できる。
- 同じ価格で毎日メールが飛ばないよう、通知済み価格を data/state.json に記録する。
- 実行ごとに data/price-log.csv へ1行追記する。これは値動きの記録を兼ねつつ、
  リポジトリに commit を発生させて schedule ワークフローの自動無効化
  （公開リポジトリは60日間 活動がないと停止）を防ぐ役割も持つ。
"""

import csv
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://banneroftruth.org/uk/store/new-release/systematic-theology/",
)
BINDING = os.environ.get("BINDING", "cloth-bound")
THRESHOLD_PENCE = int(os.environ.get("THRESHOLD_PENCE", "1800"))

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "state.json"
LOG_PATH = ROOT / "data" / "price-log.csv"

USER_AGENT = "Mozilla/5.0 (compatible; price-watch/1.0; +https://github.com/cannedcountry-star/berkhof-systematic-theology)"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_variation(product_url, binding):
    # https://banneroftruth.org/uk/store/<category>/<slug>/
    #   → API: https://banneroftruth.org/uk/wp-json/wc/store/v1/products?slug=<slug>
    base, sep, rest = product_url.partition("/store/")
    if not sep:
        raise ValueError("商品ページ URL に /store/ が含まれていません: {}".format(product_url))
    slug = rest.strip("/").split("/")[-1]
    api = base + "/wp-json/wc/store/v1/products"

    products = get_json(api + "?" + urllib.parse.urlencode({"slug": slug}))
    product = next((p for p in products if p.get("slug") == slug), None)
    if product is None:
        raise ValueError("商品が見つかりません: slug={}".format(slug))

    for v in product.get("variations", []):
        if any(a.get("value") == binding for a in v.get("attributes", [])):
            return get_json("{}/{}".format(api, v["id"]))
    raise ValueError("装丁 {} の variation が見つかりません: {}".format(
        binding, [v.get("attributes") for v in product.get("variations", [])]))


def money(pence):
    return "£{:,.2f}".format(pence / 100)


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def append_log(now, price_pence, available):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["checked_at_utc", "price_gbp", "available"])
        w.writerow([now.strftime("%Y-%m-%dT%H:%M:%SZ"), "{:.2f}".format(price_pence / 100), available])


def emit(**outputs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in outputs.items():
            print("{}={}".format(k, v))
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in outputs.items():
            f.write("{}={}\n".format(k, v))


def main():
    try:
        variation = fetch_variation(PRODUCT_URL, BINDING)
        prices = variation["prices"]
        if prices.get("currency_code") != "GBP" or prices.get("currency_minor_unit") != 2:
            raise ValueError("想定外の通貨です: {} (minor_unit={})".format(
                prices.get("currency_code"), prices.get("currency_minor_unit")))
        price_pence = int(prices["price"])
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
        # 取得失敗はジョブを失敗させる。GitHub から失敗通知メールが届くので、
        # 「壊れたまま静かに監視が止まる」状態を避けられる。
        print("ERROR: 価格の取得に失敗しました: {}".format(e), file=sys.stderr)
        return 1

    available = bool(variation.get("is_in_stock"))
    title = "{} ({})".format(html.unescape(variation.get("name", "")), html.unescape(variation.get("variation", "")))
    now = datetime.now(timezone.utc)

    print("title     : {}".format(title))
    print("price     : {} ({} pence)".format(money(price_pence), price_pence))
    print("threshold : {}".format(money(THRESHOLD_PENCE)))
    print("available : {}".format(available))

    append_log(now, price_pence, available)

    state = load_state()
    last_notified = state.get("last_notified_pence")

    below = price_pence < THRESHOLD_PENCE
    # 同じ価格を通知済みなら再通知しない。さらに下がった場合は改めて通知する。
    should_notify = below and last_notified != price_pence

    if should_notify:
        print("=> しきい値を下回りました。通知します。")
        state["last_notified_pence"] = price_pence
        state["last_notified_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif not below and last_notified is not None:
        # 値段が戻ったら通知済みフラグを解除し、次に下がったとき再び通知できるようにする。
        print("=> しきい値以上に戻りました。通知済みフラグを解除します。")
        state.pop("last_notified_pence", None)
        state.pop("last_notified_at", None)
    else:
        print("=> 通知なし。")

    state["last_checked_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["last_price_pence"] = price_pence
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    emit(
        dropped=str(should_notify).lower(),
        price=money(price_pence),
        threshold=money(THRESHOLD_PENCE),
        title=title,
        url=variation.get("permalink") or PRODUCT_URL,
        available=str(available).lower(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
