#!/usr/bin/env python3
"""
Amazon カテゴリーランキング監視ダッシュボード v2
- ASINのみ入力で全サブカテゴリを自動取得
- 前日比を含むSlack通知
- 改善されたUI/UX
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
def fetch_product_with_all_categories(api_key, asin):
    """ASINから商品情報と全カテゴリのランキングを取得"""
    if not api_key:
        return None
    
    domain_id = 5  # Amazon.co.jp
    url = f"https://api.keepa.com/product?key={api_key}&domain={domain_id}&asin={asin}&stats=1"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if 'products' not in data or len(data['products']) == 0:
            return None
        
        product = data['products'][0]
        title = product.get('title', 'Unknown Product')
        
        # カテゴリツリーからカテゴリ名を取得
        category_tree = product.get('categoryTree', [])
        category_id_to_name = {}
        for cat in category_tree:
            cat_id = str(cat.get('catId', ''))
            cat_name = cat.get('name', '')
            if cat_id and cat_name:
                category_id_to_name[cat_id] = cat_name
        
        results = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # 全カテゴリのランキングを取得
        if 'stats' in product and 'salesRank' in product['stats'] and product['stats']['salesRank']:
            sales_rank = product['stats']['salesRank']
            
            # 追加でカテゴリ名を取得（categoryTreeにないものも）
            missing_ids = [cid for cid in sales_rank.keys() if str(cid) not in category_id_to_name]
            if missing_ids:
                extra_names = fetch_category_names_batch(api_key, missing_ids)
                category_id_to_name.update(extra_names)
            
            for cat_id, rank in sales_rank.items():
                cat_name = category_id_to_name.get(str(cat_id), f"カテゴリ{cat_id}")
                if rank and rank > 0:
                    results.append({
                        'date': now,
                        'asin': asin,
                        'title': title,
                        'category_id': str(cat_id),
                        'category_name': cat_name,
                        'rank': rank
                    })
        
        return {
            'title': title,
            'asin': asin,
            'categories': len(results),
            'results': results
        }
    
    except Exception as e:
        print(f"エラー ({asin}): {e}")
        return None

def fetch_category_names_batch(api_key, category_ids):
    """複数カテゴリIDから名前を一括取得"""
    if not api_key or not category_ids:
        return {}
    
    valid_ids = []
    for cid in category_ids:
        try:
            int(cid)
            valid_ids.append(str(cid))
        except:
            continue
    
    if not valid_ids:
        return {}
    
    domain_id = 5
    url = f"https://api.keepa.com/category?key={api_key}&domain={domain_id}&category={','.join(valid_ids[:10])}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        result = {}
        if 'categories' in data:
            for cat_id, cat_info in data['categories'].items():
                result[str(cat_id)] = cat_info.get('name', f"カテゴリ{cat_id}")
        return result
    except:
        return {}

# --- Slack通知 ---
def send_slack_notification(webhook_url, all_results, df_history):
    """改善されたSlack通知（前日比付き）"""
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
            "text": {"type": "plain_text", "text": f"📊 ランキングレポート ({now.strftime('%Y-%m-%d %H:%M')})", "emoji": True}
        },
        {"type": "divider"}
    ]
    
    for asin, data in by_product.items():
        title = data['title'][:50] + "..." if len(data['title']) > 50 else data['title']
        
        lines = [f"*{title}*", f"ASIN: `{asin}`", ""]
        
        for r in data['rankings']:
            rank = r['rank']
            cat_name = r['category_name']
            cat_id = r['category_id']
            
            # 前日比を計算
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
                            change_text = f" 📈 +{diff}位UP"
                        elif diff < 0:
                            change_text = f" 📉 {diff}位DOWN"
                        else:
                            change_text = " → 変動なし"
            
            emoji = "🥇" if rank <= 10 else "🥈" if rank <= 50 else "🥉" if rank <= 100 else "📍"
            lines.append(f"{emoji} {cat_name}: *{rank:,}位*{change_text}")
        
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)}
        })
        blocks.append({"type": "divider"})
    
    payload = {"blocks": blocks}
    
    try:
        requests.post(webhook_url, json=payload)
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
        
        result = fetch_product_with_all_categories(config["api_key"], asin)
        if result and result['results']:
            all_results.extend(result['results'])
            # 商品名を更新
            product['title'] = result['title']
    
    # 商品リストを更新（タイトル追加）
    save_products(products)
    
    # データ保存
    if all_results:
        new_df = pd.DataFrame(all_results)
        df = pd.concat([df, new_df], ignore_index=True)
        save_data(df)
        
        # Slack通知
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
    
    # カスタムCSS
    st.markdown("""
    <style>
        .main-header {
            font-size: 2.5rem;
            font-weight: bold;
            background: linear-gradient(90deg, #FF6B6B, #4ECDC4);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 1rem;
        }
        .metric-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 1rem;
            border-radius: 10px;
            color: white;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 24px;
        }
        .stTabs [data-baseweb="tab"] {
            height: 50px;
            padding: 10px 24px;
            font-weight: 600;
        }
    </style>
    """, unsafe_allow_html=True)
    
    # ヘッダー
    st.markdown('<p class="main-header">📊 Amazon Ranking Monitor</p>', unsafe_allow_html=True)
    
    # スケジューラー初期化
    sched = init_scheduler()
    
    config = load_config()
    products = load_products()
    df = load_data()
    
    # --- メトリクス表示 ---
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("📦 登録商品数", len(products))
    with col2:
        st.metric("📈 データ件数", len(df))
    with col3:
        if sched and sched.running:
            next_run = sched.get_job('daily_ranking_job').next_run_time
            st.metric("⏰ 次回実行", next_run.strftime('%m/%d %H:%M') if next_run else "-")
        else:
            st.metric("⏰ 次回実行", "未設定")
    with col4:
        if not df.empty:
            latest_date = df['date'].max()
            st.metric("🕐 最終更新", latest_date[:10] if latest_date else "-")
        else:
            st.metric("🕐 最終更新", "-")
    
    st.divider()
    
    # --- タブ ---
    tab1, tab2, tab3, tab4 = st.tabs(["🏠 ダッシュボード", "📦 商品管理", "📈 推移グラフ", "⚙️ 設定"])
    
    # --- タブ1: ダッシュボード ---
    with tab1:
        col_left, col_right = st.columns([3, 1])
        
        with col_right:
            if st.button("🔄 今すぐ取得", type="primary", use_container_width=True):
                if not config.get("api_key"):
                    st.error("⚠️ APIキーを設定してください")
                elif not products:
                    st.error("⚠️ 商品を登録してください")
                else:
                    with st.spinner("Keepaからデータを取得中..."):
                        results = fetch_all_rankings()
                        if results:
                            st.success(f"✅ {len(results)}件のデータを取得しました")
                            st.rerun()
                        else:
                            st.warning("データが取得できませんでした")
        
        with col_left:
            st.subheader("📋 最新ランキング")
        
        if not df.empty:
            # 最新データを商品ごとに表示
            for product in products:
                asin = product.get('asin')
                title = product.get('title', asin)
                
                product_df = df[df['asin'] == asin]
                if product_df.empty:
                    continue
                
                with st.expander(f"📦 {title[:60]}{'...' if len(title) > 60 else ''}", expanded=True):
                    # 最新のデータを取得
                    latest_date = product_df['date'].max()
                    latest = product_df[product_df['date'] == latest_date]
                    
                    # カテゴリごとにカードで表示
                    cols = st.columns(min(len(latest), 4))
                    for i, (_, row) in enumerate(latest.iterrows()):
                        with cols[i % 4]:
                            rank = int(row['rank']) if pd.notna(row['rank']) else 0
                            emoji = "🥇" if rank <= 10 else "🥈" if rank <= 50 else "🥉" if rank <= 100 else "📍"
                            st.metric(
                                label=f"{emoji} {row['category_name'][:15]}",
                                value=f"{rank:,}位"
                            )
        else:
            st.info("💡 データがありません。「今すぐ取得」ボタンを押してください。")
    
    # --- タブ2: 商品管理 ---
    with tab2:
        st.subheader("➕ 新規商品を追加")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            new_asin = st.text_input(
                "ASIN",
                placeholder="例: B0CTBW1WXG",
                help="AmazonのASINコードを入力してください。カテゴリは自動取得されます。"
            )
        with col2:
            st.write("")
            st.write("")
            if st.button("➕ 追加", type="primary", use_container_width=True):
                if new_asin:
                    asin = new_asin.strip().upper()
                    # 重複チェック
                    if any(p['asin'] == asin for p in products):
                        st.error("⚠️ この商品は既に登録されています")
                    else:
                        products.append({"asin": asin, "title": ""})
                        save_products(products)
                        st.success(f"✅ {asin} を追加しました")
                        st.rerun()
                else:
                    st.error("ASINを入力してください")
        
        st.divider()
        st.subheader("📋 登録済み商品")
        
        if products:
            for i, p in enumerate(products):
                col1, col2, col3 = st.columns([4, 2, 1])
                with col1:
                    title = p.get('title') or '(商品名未取得)'
                    st.markdown(f"**{title[:50]}{'...' if len(title) > 50 else ''}**")
                with col2:
                    st.code(p['asin'])
                with col3:
                    if st.button("🗑️", key=f"del_{i}", help="この商品を削除"):
                        products.pop(i)
                        save_products(products)
                        st.rerun()
        else:
            st.info("💡 商品が登録されていません。上のフォームからASINを追加してください。")
    
    # --- タブ3: 推移グラフ ---
    with tab3:
        if not df.empty:
            # 商品選択
            product_options = {f"{p.get('title', p['asin'])} ({p['asin']})": p['asin'] for p in products}
            if product_options:
                selected_label = st.selectbox("📦 商品を選択", list(product_options.keys()))
                selected_asin = product_options[selected_label]
                
                product_df = df[df['asin'] == selected_asin]
                
                if not product_df.empty:
                    # カテゴリ選択
                    categories = product_df['category_name'].dropna().unique().tolist()
                    selected_cats = st.multiselect(
                        "📂 カテゴリを選択（複数可）",
                        categories,
                        default=categories[:3] if len(categories) > 3 else categories
                    )
                    
                    if selected_cats:
                        plot_df = product_df[product_df['category_name'].isin(selected_cats)]
                        
                        fig = px.line(
                            plot_df,
                            x="date",
                            y="rank",
                            color="category_name",
                            markers=True,
                            title="ランキング推移",
                            labels={"date": "日付", "rank": "順位", "category_name": "カテゴリ"}
                        )
                        fig.update_yaxes(autorange="reversed", title="順位（上が1位）")
                        fig.update_layout(
                            height=500,
                            hovermode="x unified",
                            legend=dict(orientation="h", yanchor="bottom", y=1.02)
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("カテゴリを選択してください")
        else:
            st.info("💡 データがありません。まず商品を登録してランキングを取得してください。")
        
        # CSVダウンロード
        if not df.empty:
            st.divider()
            csv_data = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                "📥 全データをCSVでダウンロード",
                csv_data,
                f"ranking_data_{datetime.now().strftime('%Y%m%d')}.csv",
                "text/csv",
                use_container_width=True
            )
    
    # --- タブ4: 設定 ---
    with tab4:
        # パスワード保護
        if 'settings_unlocked' not in st.session_state:
            st.session_state.settings_unlocked = False
        
        if not st.session_state.settings_unlocked:
            st.warning("🔒 設定を変更するにはパスワードが必要です")
            col1, col2 = st.columns([3, 1])
            with col1:
                password = st.text_input("パスワード", type="password")
            with col2:
                st.write("")
                st.write("")
                if st.button("🔓 解除", use_container_width=True):
                    if password == SETTINGS_PASSWORD:
                        st.session_state.settings_unlocked = True
                        st.rerun()
                    else:
                        st.error("パスワードが違います")
        else:
            st.success("🔓 設定が編集可能です")
            
            if st.button("🔒 ロックする"):
                st.session_state.settings_unlocked = False
                st.rerun()
            
            st.divider()
            
            st.subheader("🔑 API設定")
            api_key = st.text_input("Keepa API Key", value=config.get("api_key", ""), type="password")
            slack_url = st.text_input("Slack Webhook URL", value=config.get("slack_url", ""))
            
            if st.button("💾 設定を保存", type="primary"):
                save_config({"api_key": api_key, "slack_url": slack_url})
                st.success("✅ 保存しました")
                st.rerun()
            
            st.divider()
            
            st.subheader("⚠️ データ管理")
            if st.button("🗑️ 全データを削除", type="secondary"):
                if os.path.exists(DATA_FILE):
                    os.remove(DATA_FILE)
                st.success("データを削除しました")
                st.rerun()


if __name__ == "__main__":
    main()
