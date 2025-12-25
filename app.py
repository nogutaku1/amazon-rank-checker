#!/usr/bin/env python3
"""
Amazon カテゴリーランキング監視ダッシュボード v3
- ASINのみ入力で最も詳細なサブカテゴリを自動特定
- Best Sellers APIでランキングリストから順位を取得
- 前日比を含むSlack通知
"""

import streamlit as st
import pandas as pd
import requests
import json
import os
from datetime import datetime, timedelta
import plotly.express as px
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import threading
import atexit

# --- 設定 ---
DATA_FILE = 'ranking_data.csv'
CONFIG_FILE = 'config.json'
PRODUCTS_FILE = 'products.json'
SETTINGS_PASSWORD = "amznrnk"
DOMAIN_ID = 5  # Amazon.co.jp

# --- グローバルスケジューラー ---
scheduler = None
scheduler_lock = threading.Lock()

# --- ユーティリティ関数 ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"api_key": "", "slack_url": ""}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def load_products():
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_products(products):
    with open(PRODUCTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(products, f, indent=4, ensure_ascii=False)

def load_data():
    if os.path.exists(DATA_FILE):
        return pd.read_csv(DATA_FILE)
    return pd.DataFrame(columns=["date", "asin", "title", "category_id", "category_name", "rank"])

def save_data(df):
    df.to_csv(DATA_FILE, index=False)

# --- Keepa API関数 ---
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
            'categories': product.get('categories', []),  # カテゴリIDの配列（末尾が最も詳細）
            'categoryTree': product.get('categoryTree', []),  # カテゴリ名付きツリー
            'salesRanks': product.get('stats', {}).get('salesRank', {})  # 従来のsalesRank
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
        
        # bestSellersList にASINのリストが入っている
        if 'bestSellersList' in data and 'asinList' in data['bestSellersList']:
            asin_list = data['bestSellersList']['asinList']
            
            # 対象ASINの位置を探す
            try:
                index = asin_list.index(target_asin)
                return index + 1  # 0-indexed なので +1
            except ValueError:
                return None  # リストに見つからない（圏外）
        
        return None
    except Exception as e:
        print(f"Best Sellers API エラー: {e}")
        return None

def fetch_ranking_for_product(api_key, asin):
    """
    1つの商品について、所属するサブカテゴリでの順位を取得
    """
    # 1. 商品情報を取得
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
        # 末尾から最大3つのカテゴリを試す
        for i, cat_id in enumerate(reversed(categories[:5])):
            cat_id = str(cat_id)
            
            # カテゴリ名を取得
            cat_name = None
            for tree_item in category_tree:
                if str(tree_item.get('catId')) == cat_id:
                    cat_name = tree_item.get('name')
                    break
            
            if not cat_name:
                cat_name = get_category_name(api_key, cat_id)
            
            # Best Sellers APIでランキング取得
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
            
            # 既に追加済みならスキップ
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

# --- Slack通知 ---
def send_slack_notification(webhook_url, all_results, df_history):
    """前日比付きSlack通知"""
    if not webhook_url or not all_results:
        return
    
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    
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
            "text": {"type": "plain_text", "text": f"📊 ランキングレポート ({now.strftime('%m/%d %H:%M')})", "emoji": True}
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
            cat_id = r['category_id']
            source = r.get('source', '')
            
            # 前日比計算
            change_text = ""
            if not df_history.empty:
                prev = df_history[
                    (df_history['asin'] == asin) & 
                    (df_history['category_id'] == str(cat_id)) &
                    (df_history['date'].str.startswith(yesterday))
                ]
                if not prev.empty:
                    prev_rank = prev.iloc[-1]['rank']
                    if pd.notna(prev_rank):
                        diff = int(prev_rank) - int(rank)
                        if diff > 0:
                            change_text = f" 📈 {diff}位UP!"
                        elif diff < 0:
                            change_text = f" 📉 {abs(diff)}位DOWN"
                        else:
                            change_text = " → 変動なし"
            
            emoji = "🥇" if rank <= 10 else "🥈" if rank <= 50 else "🥉" if rank <= 100 else "📍"
            source_tag = " [BS]" if source == 'bestsellers' else ""
            lines.append(f"{emoji} {cat_name}: *{rank:,}位*{change_text}{source_tag}")
        
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })
    
    try:
        requests.post(webhook_url, json={"blocks": blocks})
        print("Slack通知完了")
    except Exception as e:
        print(f"Slack通知エラー: {e}")

# --- メイン処理 ---
def fetch_all_rankings():
    """全商品のランキングを取得"""
    print(f"[{datetime.now()}] ランキング取得開始")
    
    config = load_config()
    products = load_products()
    
    if not config.get("api_key") or not products:
        print("APIキーまたは商品リストが未設定")
        return []
    
    df = load_data()
    all_results = []
    
    for product in products:
        asin = product.get('asin')
        if not asin:
            continue
        
        print(f"取得中: {asin}")
        result = fetch_ranking_for_product(config["api_key"], asin)
        
        if result:
            all_results.extend(result['results'])
            product['title'] = result['title']
    
    save_products(products)
    
    if all_results:
        new_df = pd.DataFrame(all_results)
        # source列があれば削除（保存時は不要）
        if 'source' in new_df.columns:
            new_df = new_df.drop(columns=['source'])
        df = pd.concat([df, new_df], ignore_index=True)
        save_data(df)
        
        send_slack_notification(config.get("slack_url"), all_results, df)
    
    print(f"[{datetime.now()}] ランキング取得完了: {len(all_results)}件")
    return all_results

def init_scheduler():
    global scheduler
    with scheduler_lock:
        if scheduler is None:
            scheduler = BackgroundScheduler(timezone='Asia/Tokyo')
            scheduler.add_job(fetch_all_rankings, CronTrigger(hour=10, minute=0), id='daily_ranking_job')
            scheduler.start()
            atexit.register(lambda: scheduler.shutdown())
            print("スケジューラー起動: 毎日10:00に実行")
    return scheduler

# --- Streamlit UI ---
def main():
    st.set_page_config(
        page_title="Amazon Ranking Monitor",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="collapsed"
    )
    
    st.markdown("""
    <style>
        .main-header {
            font-size: 2.2rem;
            font-weight: bold;
            background: linear-gradient(90deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 1rem;
        }
        .stTabs [data-baseweb="tab-list"] { gap: 20px; }
        .stTabs [data-baseweb="tab"] { height: 45px; padding: 8px 20px; font-weight: 600; }
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown('<p class="main-header">📊 Amazon Ranking Monitor</p>', unsafe_allow_html=True)
    
    sched = init_scheduler()
    config = load_config()
    products = load_products()
    df = load_data()
    
    # メトリクス
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📦 登録商品", len(products))
    col2.metric("📈 データ件数", len(df))
    if sched and sched.running:
        next_run = sched.get_job('daily_ranking_job').next_run_time
        col3.metric("⏰ 次回実行", next_run.strftime('%m/%d %H:%M') if next_run else "-")
    else:
        col3.metric("⏰ 次回実行", "-")
    col4.metric("🕐 最終更新", df['date'].max()[:10] if not df.empty else "-")
    
    st.divider()
    
    # タブ
    tab1, tab2, tab3, tab4 = st.tabs(["🏠 ダッシュボード", "📦 商品管理", "📈 推移グラフ", "⚙️ 設定"])
    
    # --- ダッシュボード ---
    with tab1:
        col_left, col_right = st.columns([4, 1])
        
        with col_right:
            if st.button("🔄 今すぐ取得", type="primary", use_container_width=True):
                if not config.get("api_key"):
                    st.error("⚠️ APIキーを設定してください")
                elif not products:
                    st.error("⚠️ 商品を登録してください")
                else:
                    with st.spinner("Keepaからデータを取得中...（Best Sellers API使用）"):
                        results = fetch_all_rankings()
                        if results:
                            st.success(f"✅ {len(results)}件のランキングを取得しました")
                            st.rerun()
                        else:
                            st.warning("データが取得できませんでした")
        
        with col_left:
            st.subheader("📋 最新ランキング")
        
        if not df.empty and products:
            for product in products:
                asin = product.get('asin')
                title = product.get('title') or asin
                
                product_df = df[df['asin'] == asin]
                if product_df.empty:
                    continue
                
                with st.expander(f"📦 {title[:55]}{'...' if len(title) > 55 else ''}", expanded=True):
                    latest_date = product_df['date'].max()
                    latest = product_df[product_df['date'] == latest_date]
                    
                    if not latest.empty:
                        cols = st.columns(min(len(latest), 4))
                        for i, (_, row) in enumerate(latest.iterrows()):
                            with cols[i % 4]:
                                rank = int(row['rank']) if pd.notna(row['rank']) else 0
                                emoji = "🥇" if rank <= 10 else "🥈" if rank <= 50 else "🥉" if rank <= 100 else "📍"
                                cat_name = row['category_name'][:12] + "..." if len(str(row['category_name'])) > 12 else row['category_name']
                                st.metric(f"{emoji} {cat_name}", f"{rank:,}位")
        else:
            st.info("💡 商品を登録して「今すぐ取得」ボタンを押してください")
    
    # --- 商品管理 ---
    with tab2:
        st.subheader("➕ 商品を追加")
        
        col1, col2 = st.columns([4, 1])
        with col1:
            new_asin = st.text_input("ASIN", placeholder="例: B0CTBW1WXG", help="カテゴリは自動取得されます")
        with col2:
            st.write("")
            st.write("")
            if st.button("追加", type="primary", use_container_width=True):
                if new_asin:
                    asin = new_asin.strip().upper()
                    if any(p['asin'] == asin for p in products):
                        st.error("既に登録済みです")
                    else:
                        products.append({"asin": asin, "title": ""})
                        save_products(products)
                        st.success(f"✅ {asin} を追加しました")
                        st.rerun()
        
        st.divider()
        st.subheader("📋 登録済み商品")
        
        if products:
            for i, p in enumerate(products):
                col1, col2, col3 = st.columns([5, 2, 1])
                col1.write(f"**{p.get('title') or '(未取得)'}**")
                col2.code(p['asin'])
                if col3.button("🗑️", key=f"del_{i}"):
                    products.pop(i)
                    save_products(products)
                    st.rerun()
        else:
            st.info("商品が登録されていません")
    
    # --- 推移グラフ ---
    with tab3:
        if not df.empty and products:
            product_options = {f"{p.get('title', p['asin'])} ({p['asin']})": p['asin'] for p in products if p.get('title')}
            if product_options:
                selected_label = st.selectbox("商品を選択", list(product_options.keys()))
                selected_asin = product_options[selected_label]
                
                product_df = df[df['asin'] == selected_asin]
                
                if not product_df.empty:
                    categories = product_df['category_name'].dropna().unique().tolist()
                    selected_cats = st.multiselect("カテゴリを選択", categories, default=categories[:3])
                    
                    if selected_cats:
                        plot_df = product_df[product_df['category_name'].isin(selected_cats)]
                        
                        fig = px.line(plot_df, x="date", y="rank", color="category_name",
                                     markers=True, title="ランキング推移")
                        fig.update_yaxes(autorange="reversed", title="順位")
                        fig.update_layout(height=450, hovermode="x unified",
                                         legend=dict(orientation="h", y=1.02))
                        st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("データがありません")
        
        if not df.empty:
            st.divider()
            csv = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button("📥 CSVダウンロード", csv, f"ranking_{datetime.now().strftime('%Y%m%d')}.csv", use_container_width=True)
    
    # --- 設定 ---
    with tab4:
        if 'settings_unlocked' not in st.session_state:
            st.session_state.settings_unlocked = False
        
        if not st.session_state.settings_unlocked:
            st.warning("🔒 設定を変更するにはパスワードが必要です")
            col1, col2 = st.columns([3, 1])
            password = col1.text_input("パスワード", type="password")
            col2.write("")
            col2.write("")
            if col2.button("解除", use_container_width=True):
                if password == SETTINGS_PASSWORD:
                    st.session_state.settings_unlocked = True
                    st.rerun()
                else:
                    st.error("パスワードが違います")
        else:
            st.success("🔓 設定編集可能")
            if st.button("🔒 ロック"):
                st.session_state.settings_unlocked = False
                st.rerun()
            
            st.divider()
            api_key = st.text_input("Keepa API Key", value=config.get("api_key", ""), type="password")
            slack_url = st.text_input("Slack Webhook URL", value=config.get("slack_url", ""))
            
            if st.button("💾 保存", type="primary"):
                save_config({"api_key": api_key, "slack_url": slack_url})
                st.success("保存しました")
                st.rerun()
            
            st.divider()
            if st.button("🗑️ 全データ削除"):
                if os.path.exists(DATA_FILE):
                    os.remove(DATA_FILE)
                st.success("削除しました")
                st.rerun()


if __name__ == "__main__":
    main()
