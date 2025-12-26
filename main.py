#!/usr/bin/env python3
"""
Amazon カテゴリーランキング巡回ツール
GitHub Actionsで毎日自動実行し、登録商品のランキングをSlackに通知

v2: app.py と同じロジックを使用（カテゴリを動的に取得）
"""

import os
import json
import requests
from datetime import datetime

# --- 環境変数から読み込み ---
KEEPA_API_KEY = os.environ.get('KEEPA_API_KEY', '')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')

# --- 設定 ---
PRODUCTS_FILE = 'products.json'
DOMAIN_ID = 5  # Amazon.co.jp


def load_products():
    """監視対象商品リストを読み込み"""
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_products(products):
    """商品リストを保存（タイトル更新用）"""
    with open(PRODUCTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(products, f, indent=4, ensure_ascii=False)


def get_product_info(api_key, asin):
    """商品情報とカテゴリを取得"""
    url = f"https://api.keepa.com/product?key={api_key}&domain={DOMAIN_ID}&asin={asin}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'products' not in data or len(data['products']) == 0:
            return None
        
        product = data['products'][0]
        return {
            'asin': asin,
            'title': product.get('title', 'Unknown Product'),
            'categories': product.get('categories', []),
            'categoryTree': product.get('categoryTree', []),
            'salesRanks': product.get('stats', {}).get('salesRank', {})
        }
    except Exception as e:
        print(f"商品情報取得エラー ({asin}): {e}")
        return None


def get_category_name(api_key, category_id):
    """カテゴリIDからカテゴリ名を取得"""
    url = f"https://api.keepa.com/category?key={api_key}&domain={DOMAIN_ID}&category={category_id}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'categories' in data and str(category_id) in data['categories']:
            return data['categories'][str(category_id)].get('name', f'カテゴリ{category_id}')
        return f'カテゴリ{category_id}'
    except:
        return f'カテゴリ{category_id}'


def get_bestseller_ranking(api_key, category_id, target_asin):
    """
    Best Sellers APIでカテゴリのランキングリストを取得し、
    対象ASINの順位を返す
    """
    url = f"https://api.keepa.com/bestsellers?key={api_key}&domain={DOMAIN_ID}&category={category_id}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'bestSellersList' in data and 'asinList' in data['bestSellersList']:
            asin_list = data['bestSellersList']['asinList']
            
            try:
                index = asin_list.index(target_asin)
                return index + 1
            except ValueError:
                return None
        
        return None
    except Exception as e:
        print(f"Best Sellers API エラー: {e}")
        return None


def fetch_ranking_for_product(api_key, asin):
    """
    1つの商品について、所属するサブカテゴリでの順位を取得
    """
    product_info = get_product_info(api_key, asin)
    if not product_info:
        return None
    
    title = product_info['title']
    categories = product_info['categories']
    category_tree = product_info['categoryTree']
    sales_ranks = product_info['salesRanks']
    
    results = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 方法1: categoriesの末尾（最も詳細なサブカテゴリ）を使用
    if categories:
        for i, cat_id in enumerate(reversed(categories[:5])):
            cat_id = str(cat_id)
            
            cat_name = None
            for tree_item in category_tree:
                if str(tree_item.get('catId')) == cat_id:
                    cat_name = tree_item.get('name')
                    break
            
            if not cat_name:
                cat_name = get_category_name(api_key, cat_id)
            
            rank = get_bestseller_ranking(api_key, cat_id, asin)
            
            if rank:
                results.append({
                    'date': now,
                    'asin': asin,
                    'title': title,
                    'category_id': cat_id,
                    'category_name': cat_name,
                    'rank': rank,
                    'source': 'bestsellers'
                })
    
    # 方法2: salesRankからも取得（フォールバック）
    if sales_ranks:
        for cat_id, rank in sales_ranks.items():
            cat_id = str(cat_id)
            
            if any(r['category_id'] == cat_id for r in results):
                continue
            
            cat_name = None
            for tree_item in category_tree:
                if str(tree_item.get('catId')) == cat_id:
                    cat_name = tree_item.get('name')
                    break
            
            if not cat_name:
                cat_name = get_category_name(api_key, cat_id)
            
            if rank and rank > 0:
                results.append({
                    'date': now,
                    'asin': asin,
                    'title': title,
                    'category_id': cat_id,
                    'category_name': cat_name,
                    'rank': rank,
                    'source': 'salesRank'
                })
    
    return {
        'title': title,
        'asin': asin,
        'results': results
    }


def send_slack_notification(all_results):
    """Slackに結果を通知"""
    if not SLACK_WEBHOOK_URL:
        print("警告: SLACK_WEBHOOK_URLが設定されていません。通知をスキップします。")
        return
    
    if not all_results:
        print("通知するデータがありません")
        return
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 商品ごとにグループ化
    by_product = {}
    for r in all_results:
        asin = r['asin']
        if asin not in by_product:
            by_product[asin] = {'title': r['title'], 'rankings': []}
        by_product[asin]['rankings'].append(r)
    
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 ランキングレポート ({now})", "emoji": True}
        }
    ]
    
    for asin, data in by_product.items():
        title = data['title'][:45] + "..." if len(data['title']) > 45 else data['title']
        amazon_url = f"https://www.amazon.co.jp/dp/{asin}"
        
        lines = [
            f"*{title}*",
            f"<{amazon_url}|Amazon商品ページ>",
            ""
        ]
        
        for r in data['rankings']:
            rank = r['rank']
            cat_name = r['category_name']
            source = r.get('source', '')
            
            emoji = "🥇" if rank <= 10 else "🥈" if rank <= 50 else "🥉" if rank <= 100 else "📍"
            source_tag = " [BS]" if source == 'bestsellers' else ""
            lines.append(f"{emoji} {cat_name}: *{rank:,}位*{source_tag}")
        
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })
    
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks})
        response.raise_for_status()
        print("Slack通知完了")
    except Exception as e:
        print(f"Slack通知エラー: {e}")


def main():
    print("=" * 50)
    print("Amazon カテゴリーランキング巡回ツール v2")
    print("=" * 50)
    
    if not KEEPA_API_KEY:
        print("エラー: KEEPA_API_KEYが設定されていません")
        return
    
    # 商品リスト読み込み
    products = load_products()
    
    if not products:
        print("エラー: products.jsonが空か、存在しません")
        return
    
    print(f"監視対象: {len(products)}商品")
    
    # 全商品のランキングを取得
    all_results = []
    
    for product in products:
        asin = product.get('asin')
        if not asin:
            continue
        
        print(f"取得中: {asin}")
        result = fetch_ranking_for_product(KEEPA_API_KEY, asin)
        
        if result:
            all_results.extend(result['results'])
            # タイトルを更新
            product['title'] = result['title']
            print(f"  → {result['title'][:40]}... ({len(result['results'])}カテゴリ)")
        else:
            print(f"  → 取得失敗")
    
    # 商品リストを保存（タイトル更新）
    save_products(products)
    
    # Slack通知
    if all_results:
        print(f"\n合計 {len(all_results)} 件のランキングを取得")
        send_slack_notification(all_results)
    else:
        print("\nランキング取得結果がありません")
    
    print("\n完了！")


if __name__ == "__main__":
    main()
