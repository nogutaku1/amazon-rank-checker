#!/usr/bin/env python3
"""
Amazon カテゴリーランキング巡回ツール
GitHub Actionsで毎日自動実行し、指定カテゴリのランキングTOP10をSlackに通知
"""

import os
import json
import requests
from datetime import datetime

# --- 環境変数から読み込み ---
KEEPA_API_KEY = os.environ.get('KEEPA_API_KEY', '')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')

# --- 設定ファイルパス ---
PRODUCTS_FILE = 'products.json'


def load_products():
    """監視対象商品リストを読み込み"""
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def fetch_category_name(api_key, category_id):
    """Keepa Category APIからカテゴリ名を取得"""
    if not api_key or not category_id:
        return f"カテゴリID:{category_id}"
    
    domain_id = 5  # Amazon.co.jp
    url = f"https://api.keepa.com/category?key={api_key}&domain={domain_id}&category={category_id}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'categories' in data and str(category_id) in data['categories']:
            return data['categories'][str(category_id)].get('name', f"カテゴリID:{category_id}")
        return f"カテゴリID:{category_id}"
    except Exception as e:
        print(f"カテゴリ取得エラー: {e}")
        return f"カテゴリID:{category_id}"


def fetch_ranking(api_key, asin, category_id):
    """指定ASINの指定カテゴリでのランキングを取得"""
    if not api_key:
        print("エラー: KEEPA_API_KEYが設定されていません")
        return None
    
    domain_id = 5  # Amazon.co.jp
    url = f"https://api.keepa.com/product?key={api_key}&domain={domain_id}&asin={asin}&stats=1"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'products' not in data or len(data['products']) == 0:
            print(f"商品が見つかりません: {asin}")
            return None
        
        product = data['products'][0]
        title = product.get('title', 'Unknown Product')
        
        # 指定カテゴリのランキングを探す
        rank = None
        if 'stats' in product and 'salesRank' in product['stats']:
            sales_rank = product['stats']['salesRank']
            if sales_rank and str(category_id) in sales_rank:
                rank = sales_rank[str(category_id)]
            elif sales_rank and int(category_id) in sales_rank:
                rank = sales_rank[int(category_id)]
        
        return {
            'asin': asin,
            'title': title,
            'category_id': category_id,
            'rank': rank
        }
    
    except Exception as e:
        print(f"ランキング取得エラー ({asin}): {e}")
        return None


def send_slack_notification(results, category_name):
    """Slackに結果を通知"""
    if not SLACK_WEBHOOK_URL:
        print("警告: SLACK_WEBHOOK_URLが設定されていません。通知をスキップします。")
        return
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # ランキングでソート（Noneは最後に）
    sorted_results = sorted(
        results, 
        key=lambda x: x['rank'] if x['rank'] is not None else float('inf')
    )
    
    # メッセージ作成
    lines = [f"📊 *{category_name} ランキング* ({now})"]
    lines.append("")
    
    for i, item in enumerate(sorted_results[:10], 1):  # TOP10
        rank = item['rank']
        title = item['title'][:40] + "..." if len(item['title']) > 40 else item['title']
        
        if rank:
            emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "📍"
            lines.append(f"{emoji} *{rank}位* - {title}")
        else:
            lines.append(f"❓ *圏外* - {title}")
    
    message = "\n".join(lines)
    
    payload = {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": message}}
        ]
    }
    
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        print("Slack通知完了")
    except Exception as e:
        print(f"Slack通知エラー: {e}")


def main():
    print("=" * 50)
    print("Amazon カテゴリーランキング巡回ツール")
    print("=" * 50)
    
    # 商品リスト読み込み
    products = load_products()
    
    if not products:
        print("エラー: products.jsonが空か、存在しません")
        return
    
    print(f"監視対象: {len(products)}商品")
    
    # カテゴリごとにグループ化
    category_groups = {}
    for product in products:
        cat_id = product.get('category_id')
        if cat_id not in category_groups:
            category_groups[cat_id] = []
        category_groups[cat_id].append(product)
    
    # カテゴリごとにランキング取得・通知
    for category_id, items in category_groups.items():
        print(f"\n--- カテゴリ {category_id} の処理 ---")
        
        # カテゴリ名取得
        category_name = fetch_category_name(KEEPA_API_KEY, category_id)
        print(f"カテゴリ名: {category_name}")
        
        # ランキング取得
        results = []
        for item in items:
            asin = item.get('asin')
            name = item.get('name', asin)
            print(f"取得中: {name} ({asin})")
            
            result = fetch_ranking(KEEPA_API_KEY, asin, category_id)
            if result:
                results.append(result)
        
        # Slack通知
        if results:
            send_slack_notification(results, category_name)
        else:
            print("ランキング取得結果がありません")
    
    print("\n完了！")


if __name__ == "__main__":
    main()
