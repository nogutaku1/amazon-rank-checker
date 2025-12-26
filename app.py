#!/usr/bin/env python3
"""
Amazon カテゴリーランキング監視ダッシュボード v5
- Supabaseでデータを永続化
- 改善されたUI/UX
"""

import streamlit as st
import pandas as pd
import requests
import os
from datetime import datetime, timedelta
import plotly.express as px
from supabase import create_client, Client

# --- Supabase設定 ---
SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.environ.get("SUPABASE_URL", ""))
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", os.environ.get("SUPABASE_KEY", ""))

# --- 設定 ---
DOMAIN_ID = 5  # Amazon.co.jp

# --- グローバルログ ---
_log_messages = []
_api_errors = []

def add_log(msg):
    _log_messages.append(msg)
    print(msg)

def get_logs():
    return _log_messages

def clear_logs():
    global _log_messages
    _log_messages = []

def get_api_errors():
    return _api_errors

def clear_api_errors():
    global _api_errors
    _api_errors = []

# --- Supabaseクライアント ---
@st.cache_resource
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# --- データベース操作関数 ---
def load_products():
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        response = supabase.table('products').select('*').order('created_at').execute()
        return [{"asin": p['asin'], "title": p.get('title', '')} for p in response.data]
    except Exception as e:
        st.error(f"商品リスト取得エラー: {e}")
        return []

def save_product(asin: str, title: str = ""):
    supabase = get_supabase_client()
    if not supabase:
        return False
    try:
        supabase.table('products').upsert({"asin": asin, "title": title}).execute()
        return True
    except Exception as e:
        st.error(f"商品追加エラー: {e}")
        return False

def update_product_title(asin: str, title: str):
    supabase = get_supabase_client()
    if not supabase:
        return
    try:
        supabase.table('products').update({"title": title}).eq('asin', asin).execute()
    except:
        pass

def delete_product(asin: str):
    supabase = get_supabase_client()
    if not supabase:
        return False
    try:
        supabase.table('products').delete().eq('asin', asin).execute()
        return True
    except Exception as e:
        st.error(f"商品削除エラー: {e}")
        return False

def load_data():
    supabase = get_supabase_client()
    if not supabase:
        return pd.DataFrame(columns=["date", "asin", "title", "category_id", "category_name", "rank"])
    try:
        response = supabase.table('ranking_data').select('*').order('date', desc=True).limit(5000).execute()
        if response.data:
            df = pd.DataFrame(response.data)
            return df[["date", "asin", "title", "category_id", "category_name", "rank"]]
        return pd.DataFrame(columns=["date", "asin", "title", "category_id", "category_name", "rank"])
    except Exception as e:
        st.error(f"ランキングデータ取得エラー: {e}")
        return pd.DataFrame(columns=["date", "asin", "title", "category_id", "category_name", "rank"])

def save_ranking_data(results: list):
    supabase = get_supabase_client()
    if not supabase or not results:
        return
    try:
        data = [{k: v for k, v in r.items() if k != 'source'} for r in results]
        supabase.table('ranking_data').insert(data).execute()
    except Exception as e:
        st.error(f"ランキングデータ保存エラー: {e}")

def load_config():
    return {
        "api_key": st.secrets.get("KEEPA_API_KEY", os.environ.get("KEEPA_API_KEY", "")),
        "slack_url": st.secrets.get("SLACK_WEBHOOK_URL", os.environ.get("SLACK_WEBHOOK_URL", ""))
    }

# --- Keepa API関数 ---
def get_product_info(api_key, asin):
    global _api_errors
    url = f"https://api.keepa.com/product?key={api_key}&domain={DOMAIN_ID}&asin={asin}"
    
    try:
        response = requests.get(url, timeout=30)
        
        if response.status_code != 200:
            error_msg = f"API Error {response.status_code}"
            _api_errors.append(f"{asin}: {error_msg}")
            return None
        
        data = response.json()
        
        if 'error' in data:
            _api_errors.append(f"{asin}: {data['error'].get('message', 'Unknown error')}")
            return None
        
        if 'products' not in data or len(data['products']) == 0:
            _api_errors.append(f"{asin}: 商品が見つかりません")
            return None
        
        product = data['products'][0]
        return {
            'asin': asin,
            'title': product.get('title', 'Unknown Product'),
            'categories': product.get('categories', []),
            'categoryTree': product.get('categoryTree', []),
            'salesRanks': product.get('stats', {}).get('salesRank', {})
        }
    except requests.exceptions.Timeout:
        _api_errors.append(f"{asin}: タイムアウト")
        return None
    except Exception as e:
        _api_errors.append(f"{asin}: {str(e)[:50]}")
        return None

def get_category_name(api_key, category_id):
    url = f"https://api.keepa.com/category?key={api_key}&domain={DOMAIN_ID}&category={category_id}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if 'categories' in data and str(category_id) in data['categories']:
            return data['categories'][str(category_id)].get('name', f'カテゴリ{category_id}')
        return f'カテゴリ{category_id}'
    except:
        return f'カテゴリ{category_id}'

def get_bestseller_ranking(api_key, category_id, target_asin):
    url = f"https://api.keepa.com/bestsellers?key={api_key}&domain={DOMAIN_ID}&category={category_id}"
    try:
        response = requests.get(url, timeout=30)
        data = response.json()
        if 'bestSellersList' in data and 'asinList' in data['bestSellersList']:
            asin_list = data['bestSellersList']['asinList']
            try:
                return asin_list.index(target_asin) + 1
            except ValueError:
                return None
        return None
    except:
        return None

def fetch_ranking_for_product(api_key, asin):
    product_info = get_product_info(api_key, asin)
    if not product_info:
        return None
    
    title = product_info['title']
    categories = product_info['categories']
    category_tree = product_info['categoryTree']
    sales_ranks = product_info['salesRanks']
    
    results = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    if categories:
        for cat_id in list(reversed(categories[:5])):
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
                    'date': now, 'asin': asin, 'title': title,
                    'category_id': cat_id, 'category_name': cat_name,
                    'rank': rank, 'source': 'bestsellers'
                })
    
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
                    'date': now, 'asin': asin, 'title': title,
                    'category_id': cat_id, 'category_name': cat_name,
                    'rank': rank, 'source': 'salesRank'
                })
    
    return {'title': title, 'asin': asin, 'results': results}

# --- Slack通知 ---
def send_slack_notification(webhook_url, all_results, df_history):
    if not webhook_url or not all_results:
        return
    
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    
    by_product = {}
    for r in all_results:
        asin = r['asin']
        if asin not in by_product:
            by_product[asin] = {'title': r['title'], 'rankings': []}
        by_product[asin]['rankings'].append(r)
    
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"📊 ランキングレポート ({now.strftime('%m/%d %H:%M')})", "emoji": True}}]
    
    for asin, data in by_product.items():
        title = data['title'][:45] + "..." if len(data['title']) > 45 else data['title']
        lines = [f"*{title}*", f"<https://www.amazon.co.jp/dp/{asin}|Amazon>", ""]
        
        for r in data['rankings']:
            rank = r['rank']
            cat_name = r['category_name']
            emoji = "🥇" if rank <= 10 else "🥈" if rank <= 50 else "🥉" if rank <= 100 else "📍"
            lines.append(f"{emoji} {cat_name}: *{rank:,}位*")
        
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    
    try:
        requests.post(webhook_url, json={"blocks": blocks})
    except:
        pass

# --- メイン処理 ---
def fetch_all_rankings():
    clear_logs()
    clear_api_errors()
    
    add_log("🚀 ランキング取得を開始...")
    
    config = load_config()
    products = load_products()
    
    if not config.get("api_key"):
        add_log("❌ APIキーが未設定")
        return []
    
    if not products:
        add_log("❌ 商品リストが空")
        return []
    
    add_log(f"📦 {len(products)}件の商品を処理中...")
    
    df = load_data()
    all_results = []
    success_count = 0
    fail_count = 0
    
    for product in products:
        asin = product.get('asin')
        if not asin:
            continue
        
        result = fetch_ranking_for_product(config["api_key"], asin)
        
        if result and result['results']:
            add_log(f"✅ {asin}: {result['title'][:25]}... ({len(result['results'])}件)")
            all_results.extend(result['results'])
            update_product_title(asin, result['title'])
            success_count += 1
        else:
            add_log(f"❌ {asin}: 取得失敗")
            fail_count += 1
    
    # エラー詳細
    api_errors = get_api_errors()
    if api_errors:
        add_log("--- エラー詳細 ---")
        for err in api_errors[:3]:
            add_log(f"⚠️ {err[:60]}")
    
    if all_results:
        save_ranking_data(all_results)
        send_slack_notification(config.get("slack_url"), all_results, df)
    
    add_log(f"📊 完了: 成功{success_count}件 / 失敗{fail_count}件")
    return all_results

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
        /* ヘッダー */
        .main-header {
            font-size: 1.8rem;
            font-weight: 700;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }
        
        /* メトリクスカード */
        [data-testid="metric-container"] {
            background: linear-gradient(135deg, #f5f7fa 0%, #e4e8ec 100%);
            border-radius: 10px;
            padding: 15px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        }
        
        /* タブスタイル */
        .stTabs [data-baseweb="tab-list"] { 
            gap: 8px;
            background: #f8f9fa;
            padding: 5px;
            border-radius: 10px;
        }
        .stTabs [data-baseweb="tab"] { 
            height: 40px;
            padding: 0 16px;
            font-weight: 500;
            border-radius: 8px;
        }
        .stTabs [aria-selected="true"] {
            background: white !important;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        /* ログボックス */
        .log-box {
            background: #1e1e1e;
            color: #d4d4d4;
            font-family: 'SF Mono', 'Monaco', 'Menlo', monospace;
            font-size: 12px;
            padding: 12px;
            border-radius: 8px;
            height: 200px;
            overflow-y: auto;
            white-space: pre-wrap;
            line-height: 1.5;
        }
        
        /* ボタン */
        .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
            font-weight: 600;
        }
        
        /* エクスパンダー */
        .streamlit-expanderHeader {
            font-weight: 600;
            font-size: 0.95rem;
        }
    </style>
    """, unsafe_allow_html=True)
    
    # ヘッダー
    st.markdown('<p class="main-header">📊 Amazon Ranking Monitor</p>', unsafe_allow_html=True)
    
    # Supabase接続チェック
    supabase = get_supabase_client()
    if not supabase:
        st.error("⚠️ Supabaseの設定が必要です。Streamlit SecretsにSUPABASE_URLとSUPABASE_KEYを設定してください。")
        st.stop()
    
    config = load_config()
    products = load_products()
    df = load_data()
    
    # メトリクス
    cols = st.columns(4)
    cols[0].metric("📦 登録商品", len(products))
    cols[1].metric("📈 データ件数", len(df))
    cols[2].metric("💾 ストレージ", "Supabase")
    last_update = df['date'].max()[:10] if not df.empty else "-"
    cols[3].metric("🕐 最終更新", last_update)
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # タブ
    tab1, tab2, tab3, tab4 = st.tabs(["🏠 ダッシュボード", "📦 商品管理", "📈 推移グラフ", "⚙️ 設定"])
    
    # --- ダッシュボード ---
    with tab1:
        # 取得ボタンとログ
        col1, col2 = st.columns([3, 1])
        
        with col2:
            fetch_clicked = st.button("🔄 今すぐ取得", type="primary", use_container_width=True)
        
        with col1:
            if fetch_clicked:
                if not config.get("api_key"):
                    st.error("⚠️ Keepa APIキーを設定してください（設定タブ参照）")
                elif not products:
                    st.error("⚠️ 商品を登録してください")
                else:
                    with st.spinner("Keepa APIからデータを取得中..."):
                        results = fetch_all_rankings()
                    
                    # ログ表示
                    logs = get_logs()
                    if logs:
                        log_text = "\n".join(logs)
                        st.markdown(f'<div class="log-box">{log_text}</div>', unsafe_allow_html=True)
                    
                    if results:
                        st.success(f"✅ {len(results)}件のランキングを取得しました！")
                        st.button("🔄 画面を更新", on_click=lambda: st.rerun())
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("📋 最新ランキング")
        
        if not df.empty and products:
            # 商品ごとにカード表示
            for product in products:
                asin = product.get('asin')
                title = product.get('title') or asin
                
                product_df = df[df['asin'] == asin]
                if product_df.empty:
                    continue
                
                with st.expander(f"📦 {title[:50]}{'...' if len(title) > 50 else ''}", expanded=True):
                    latest_date = product_df['date'].max()
                    latest = product_df[product_df['date'] == latest_date]
                    
                    if not latest.empty:
                        num_cols = min(len(latest), 4)
                        cols = st.columns(num_cols)
                        for i, (_, row) in enumerate(latest.iterrows()):
                            with cols[i % num_cols]:
                                rank = int(row['rank']) if pd.notna(row['rank']) else 0
                                if rank <= 10:
                                    emoji, color = "🥇", "#ffd700"
                                elif rank <= 50:
                                    emoji, color = "🥈", "#c0c0c0"
                                elif rank <= 100:
                                    emoji, color = "🥉", "#cd7f32"
                                else:
                                    emoji, color = "📍", "#666"
                                
                                cat_name = str(row['category_name'])[:15]
                                st.metric(f"{emoji} {cat_name}", f"{rank:,}位")
        else:
            st.info("💡 商品を登録して「今すぐ取得」ボタンを押してください")
    
    # --- 商品管理 ---
    with tab2:
        st.subheader("➕ 商品を追加")
        
        col1, col2 = st.columns([5, 1])
        with col1:
            new_asin = st.text_input("ASIN", placeholder="例: B0CTBW1WXG", label_visibility="collapsed")
        with col2:
            if st.button("追加", type="primary", use_container_width=True):
                if new_asin:
                    asin = new_asin.strip().upper()
                    if any(p['asin'] == asin for p in products):
                        st.error("既に登録済みです")
                    elif len(asin) != 10:
                        st.error("ASINは10文字です")
                    else:
                        if save_product(asin):
                            st.success(f"✅ {asin} を追加しました")
                            st.rerun()
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader(f"📋 登録済み商品 ({len(products)}件)")
        
        if products:
            for i, p in enumerate(products):
                col1, col2, col3 = st.columns([6, 2, 1])
                title = p.get('title') or '(未取得)'
                col1.write(f"**{title[:45]}{'...' if len(title) > 45 else ''}**")
                col2.code(p['asin'], language=None)
                if col3.button("🗑️", key=f"del_{i}", help="削除"):
                    if delete_product(p['asin']):
                        st.rerun()
        else:
            st.info("商品が登録されていません。上のフォームからASINを追加してください。")
    
    # --- 推移グラフ ---
    with tab3:
        if not df.empty and products:
            product_options = {f"{p.get('title', p['asin'])[:40]} ({p['asin']})": p['asin'] 
                             for p in products if p.get('title')}
            
            if product_options:
                selected_label = st.selectbox("📦 商品を選択", list(product_options.keys()))
                selected_asin = product_options[selected_label]
                
                product_df = df[df['asin'] == selected_asin].copy()
                
                if not product_df.empty:
                    categories = product_df['category_name'].dropna().unique().tolist()
                    selected_cats = st.multiselect("📂 カテゴリを選択", categories, default=categories[:3])
                    
                    if selected_cats:
                        plot_df = product_df[product_df['category_name'].isin(selected_cats)]
                        
                        fig = px.line(plot_df, x="date", y="rank", color="category_name",
                                     markers=True, title="ランキング推移")
                        fig.update_yaxes(autorange="reversed", title="順位")
                        fig.update_layout(
                            height=400,
                            hovermode="x unified",
                            legend=dict(orientation="h", y=-0.2),
                            margin=dict(l=20, r=20, t=40, b=20)
                        )
                        st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("まず「今すぐ取得」でデータを取得してください")
        else:
            st.info("データがありません。ダッシュボードから「今すぐ取得」を実行してください。")
        
        if not df.empty:
            st.markdown("<br>", unsafe_allow_html=True)
            csv = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                "📥 CSVダウンロード", 
                csv, 
                f"ranking_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )
    
    # --- 設定 ---
    with tab4:
        st.subheader("⚙️ 設定情報")
        
        st.info("""
**設定はStreamlit Secretsで管理されています**

Streamlit Cloudのダッシュボード → Settings → Secrets で以下を設定してください：

```toml
SUPABASE_URL = "https://xxxxx.supabase.co"
SUPABASE_KEY = "eyJxxxx..."
KEEPA_API_KEY = "あなたのKeepa APIキー"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/..."  # オプション
```
        """)
        
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("📊 接続状態")
        
        col1, col2 = st.columns(2)
        with col1:
            if SUPABASE_URL and SUPABASE_KEY:
                st.success("✅ Supabase: 接続済み")
            else:
                st.error("❌ Supabase: 未設定")
        
        with col2:
            if config.get("api_key"):
                st.success("✅ Keepa API: 設定済み")
            else:
                st.error("❌ Keepa API: 未設定")


if __name__ == "__main__":
    main()
