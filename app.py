#!/usr/bin/env python3
"""
Amazon カテゴリーランキング監視ダッシュボード
- Web UIでASIN + カテゴリIDを登録
- 毎日10時に自動巡回（APScheduler）
- Slackに通知
"""

import streamlit as st
import pandas as pd
import requests
import json
import os
from datetime import datetime
import plotly.express as px
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import threading
import atexit

# --- 設定 ---
DATA_FILE = 'ranking_data.csv'
CONFIG_FILE = 'config.json'
PRODUCTS_FILE = 'products.json'

# --- 設定パスワード ---
SETTINGS_PASSWORD = "amznrnk"

# --- グローバルスケジューラー ---
scheduler = None
scheduler_lock = threading.Lock()

# --- 関数: 設定の読み書き ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"api_key": "", "slack_url": ""}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

# --- 関数: 商品リストの読み書き ---
def load_products():
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_products(products):
    with open(PRODUCTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(products, f, indent=4, ensure_ascii=False)

# --- 関数: カテゴリ名を取得 ---
def fetch_category_name(api_key, category_id):
    if not api_key or not category_id:
        return f"カテゴリID:{category_id}"
    
    domain_id = 5
    url = f"https://api.keepa.com/category?key={api_key}&domain={domain_id}&category={category_id}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'categories' in data and str(category_id) in data['categories']:
            return data['categories'][str(category_id)].get('name', f"カテゴリID:{category_id}")
        return f"カテゴリID:{category_id}"
    except:
        return f"カテゴリID:{category_id}"

# --- 関数: ランキング取得 ---
def fetch_ranking(api_key, asin, category_id):
    if not api_key:
        return None
    
    domain_id = 5
    url = f"https://api.keepa.com/product?key={api_key}&domain={domain_id}&asin={asin}&stats=1"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'products' not in data or len(data['products']) == 0:
            return None
        
        product = data['products'][0]
        title = product.get('title', 'Unknown Product')
        
        rank = None
        if 'stats' in product and 'salesRank' in product['stats']:
            sales_rank = product['stats']['salesRank']
            if sales_rank:
                if str(category_id) in sales_rank:
                    rank = sales_rank[str(category_id)]
                elif int(category_id) in sales_rank:
                    rank = sales_rank[int(category_id)]
        
        return {
            'date': datetime.now().strftime("%Y-%m-%d %H:%M"),
            'asin': asin,
            'title': title,
            'category_id': str(category_id),
            'rank': rank
        }
    except Exception as e:
        print(f"エラー: {e}")
        return None

# --- 関数: Slack通知 ---
def send_slack(webhook_url, results, category_name):
    if not webhook_url:
        return
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 *{category_name} ランキング* ({now})", ""]
    
    for item in results:
        rank = item.get('rank')
        title = item['title'][:40] + "..." if len(item['title']) > 40 else item['title']
        
        if rank:
            emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "📍"
            lines.append(f"{emoji} *{rank}位* - {title}")
        else:
            lines.append(f"❓ *圏外* - {title}")
    
    payload = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]}
    
    try:
        requests.post(webhook_url, json=payload)
    except:
        pass

# --- 関数: 全商品のランキングを取得 ---
def fetch_all_rankings():
    print(f"[{datetime.now()}] ランキング取得開始")
    config = load_config()
    products = load_products()
    
    if not config.get("api_key") or not products:
        print("APIキーまたは商品リストが未設定")
        return []
    
    # データ読み込み
    if os.path.exists(DATA_FILE):
        df = pd.read_csv(DATA_FILE)
    else:
        df = pd.DataFrame(columns=["date", "asin", "title", "category_id", "category_name", "rank"])
    
    # カテゴリごとにグループ化
    category_groups = {}
    for product in products:
        cat_id = product.get('category_id')
        if cat_id not in category_groups:
            category_groups[cat_id] = []
        category_groups[cat_id].append(product)
    
    all_results = []
    
    for category_id, items in category_groups.items():
        category_name = fetch_category_name(config["api_key"], category_id)
        results = []
        
        for item in items:
            result = fetch_ranking(config["api_key"], item['asin'], category_id)
            if result:
                result['category_name'] = category_name
                results.append(result)
                all_results.append(result)
        
        if results:
            send_slack(config.get("slack_url"), results, category_name)
    
    # データ保存
    if all_results:
        new_df = pd.DataFrame(all_results)
        df = pd.concat([df, new_df], ignore_index=True)
        df.to_csv(DATA_FILE, index=False)
    
    print(f"[{datetime.now()}] ランキング取得完了: {len(all_results)}件")
    return all_results

# --- 関数: スケジューラー初期化 ---
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

# --- メインアプリ ---
def main():
    st.set_page_config(page_title="Amazon Ranking Dashboard", page_icon="📈", layout="wide")
    st.title("📈 Amazon カテゴリーランキング監視")
    
    # スケジューラー初期化
    sched = init_scheduler()
    
    config = load_config()
    products = load_products()

    # --- サイドバー ---
    st.sidebar.header("⚙️ 設定")
    
    # パスワード保護
    if 'settings_unlocked' not in st.session_state:
        st.session_state.settings_unlocked = False
    
    if not st.session_state.settings_unlocked:
        st.sidebar.warning("🔒 設定はロックされています")
        password_input = st.sidebar.text_input("パスワード", type="password", key="pw")
        if st.sidebar.button("ロック解除"):
            if password_input == SETTINGS_PASSWORD:
                st.session_state.settings_unlocked = True
                st.rerun()
            else:
                st.sidebar.error("パスワードが違います")
    else:
        st.sidebar.success("🔓 設定ロック解除済み")
        if st.sidebar.button("🔒 ロックする"):
            st.session_state.settings_unlocked = False
            st.rerun()
        
        api_key = st.sidebar.text_input("Keepa API Key", value=config.get("api_key", ""), type="password")
        slack_url = st.sidebar.text_input("Slack Webhook URL", value=config.get("slack_url", ""))
        
        if st.sidebar.button("設定を保存"):
            save_config({"api_key": api_key, "slack_url": slack_url})
            st.sidebar.success("保存しました！")
            st.rerun()
    
    # スケジューラー状態
    st.sidebar.divider()
    st.sidebar.subheader("⏰ 自動巡回")
    if sched and sched.running:
        next_run = sched.get_job('daily_ranking_job').next_run_time
        st.sidebar.success("✅ 有効 (毎日10:00)")
        if next_run:
            st.sidebar.caption(f"次回実行: {next_run.strftime('%Y-%m-%d %H:%M')}")

    # --- メインエリア: タブ ---
    tab1, tab2, tab3 = st.tabs(["📋 商品登録", "📊 ランキング確認", "📈 推移グラフ"])
    
    # --- タブ1: 商品登録 ---
    with tab1:
        st.subheader("監視商品の登録")
        
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            new_asin = st.text_input("ASIN", placeholder="B0CTBW1WXG")
        with col2:
            new_category = st.text_input("カテゴリID", placeholder="170638011")
        with col3:
            new_name = st.text_input("商品名（メモ）", placeholder="ルームフレグランス")
        
        if st.button("➕ 商品を追加", type="primary"):
            if new_asin and new_category:
                products.append({
                    "asin": new_asin.strip(),
                    "category_id": new_category.strip(),
                    "name": new_name.strip() or new_asin
                })
                save_products(products)
                st.success(f"追加しました: {new_asin}")
                st.rerun()
            else:
                st.error("ASINとカテゴリIDは必須です")
        
        st.divider()
        st.subheader("登録済み商品")
        
        if products:
            for i, p in enumerate(products):
                col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
                col1.write(f"**{p.get('name', p['asin'])}**")
                col2.code(p['asin'])
                col3.code(p['category_id'])
                if col4.button("🗑️", key=f"del_{i}"):
                    products.pop(i)
                    save_products(products)
                    st.rerun()
        else:
            st.info("商品が登録されていません。上のフォームから追加してください。")
    
    # --- タブ2: ランキング確認 ---
    with tab2:
        st.subheader("最新ランキング")
        
        if st.button("🔄 今すぐランキングを取得", type="primary"):
            if not config.get("api_key"):
                st.error("Keepa API Keyを設定してください")
            elif not products:
                st.error("商品を登録してください")
            else:
                with st.spinner("Keepaからデータを取得中..."):
                    results = fetch_all_rankings()
                    if results:
                        st.success(f"取得完了！ {len(results)}件のデータを保存しました。")
                        st.rerun()
                    else:
                        st.warning("データが取得できませんでした。")
        
        # データ表示
        if os.path.exists(DATA_FILE):
            df = pd.read_csv(DATA_FILE)
            if not df.empty:
                # 最新データ
                latest = df.sort_values('date').groupby(['asin', 'category_id']).tail(1)
                st.dataframe(
                    latest[['title', 'category_name', 'rank', 'date']].reset_index(drop=True),
                    use_container_width=True
                )
                
                # CSVダウンロード
                st.divider()
                csv_data = df.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    "📥 CSVをダウンロード",
                    csv_data,
                    f"ranking_{datetime.now().strftime('%Y%m%d')}.csv",
                    "text/csv"
                )
            else:
                st.info("データがありません。「今すぐランキングを取得」ボタンを押してください。")
        else:
            st.info("データがありません。「今すぐランキングを取得」ボタンを押してください。")
    
    # --- タブ3: 推移グラフ ---
    with tab3:
        st.subheader("ランキング推移")
        
        if os.path.exists(DATA_FILE):
            df = pd.read_csv(DATA_FILE)
            if not df.empty and 'rank' in df.columns:
                # 商品選択
                titles = df['title'].dropna().unique().tolist()
                if titles:
                    selected = st.selectbox("商品を選択", titles)
                    plot_df = df[df['title'] == selected]
                    
                    if not plot_df.empty:
                        fig = px.line(
                            plot_df, x="date", y="rank", 
                            color="category_name" if 'category_name' in plot_df.columns else None,
                            markers=True,
                            title=f"{selected[:50]}... のランキング推移"
                        )
                        fig.update_yaxes(autorange="reversed")
                        st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("グラフ表示できるデータがありません。")
            else:
                st.info("データがありません。")
        else:
            st.info("データがありません。")


if __name__ == "__main__":
    main()
