#!/usr/bin/env python3
"""
Amazon カテゴリーランキング監視ダッシュボード v4
- Supabaseでデータを永続化
- ASINのみ入力で最も詳細なサブカテゴリを自動特定
- Best Sellers APIでランキングリストから順位を取得
- 前日比を含むSlack通知
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
SETTINGS_PASSWORD = "amznrnk"
DOMAIN_ID = 5  # Amazon.co.jp

# --- Supabaseクライアント ---
@st.cache_resource
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# --- データベース操作関数 ---
def load_products():
    """Supabaseから商品リストを取得"""
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
    """商品を追加"""
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
    """商品タイトルを更新"""
    supabase = get_supabase_client()
    if not supabase:
        return
    try:
        supabase.table('products').update({"title": title}).eq('asin', asin).execute()
    except:
        pass

def delete_product(asin: str):
    """商品を削除"""
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
    """Supabaseからランキングデータを取得"""
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
    """ランキングデータを保存"""
    supabase = get_supabase_client()
    if not supabase or not results:
        return
    try:
        # source列を除外
        data = [{k: v for k, v in r.items() if k != 'source'} for r in results]
        supabase.table('ranking_data').insert(data).execute()
    except Exception as e:
        st.error(f"ランキングデータ保存エラー: {e}")

def load_config():
    """設定を取得（Streamlit Secretsから）"""
    return {
        "api_key": st.secrets.get("KEEPA_API_KEY", os.environ.get("KEEPA_API_KEY", "")),
        "slack_url": st.secrets.get("SLACK_WEBHOOK_URL", os.environ.get("SLACK_WEBHOOK_URL", ""))
    }

# --- Keepa API関数 ---
# グローバルなエラーログ
_api_errors = []

def get_api_errors():
    return _api_errors

def clear_api_errors():
    global _api_errors
    _api_errors = []

def get_product_info(api_key, asin):
    """商品情報とカテゴリを取得"""
    global _api_errors
    url = f"https://api.keepa.com/product?key={api_key}&domain={DOMAIN_ID}&asin={asin}"
    
    try:
        response = requests.get(url, timeout=30)
        
        # レスポンスのステータスチェック
        if response.status_code != 200:
            error_msg = f"API Error {response.status_code}: {response.text[:200]}"
            _api_errors.append(f"{asin}: {error_msg}")
            print(error_msg)
            return None
        
        data = response.json()
        
        # エラーチェック
        if 'error' in data:
            error_msg = f"Keepa Error: {data['error']}"
            _api_errors.append(f"{asin}: {error_msg}")
            print(error_msg)
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
        _api_errors.append(f"{asin}: {str(e)}")
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
    """Best Sellers APIでカテゴリのランキングリストを取得"""
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
    """1つの商品について、所属するサブカテゴリでの順位を取得"""
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

# --- Slack通知 ---
def send_slack_notification(webhook_url, all_results, df_history):
    """前日比付きSlack通知"""
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
    
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 ランキングレポート ({now.strftime('%m/%d %H:%M')})", "emoji": True}
        }
    ]
    
    for asin, data in by_product.items():
        title = data['title'][:45] + "..." if len(data['title']) > 45 else data['title']
        amazon_url = f"https://www.amazon.co.jp/dp/{asin}"
        
        lines = [f"*{title}*", f"<{amazon_url}|Amazon商品ページ>", ""]
        
        for r in data['rankings']:
            rank = r['rank']
            cat_name = r['category_name']
            cat_id = r['category_id']
            source = r.get('source', '')
            
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
def fetch_all_rankings(debug_container=None):
    """全商品のランキングを取得"""
    def debug(msg):
        print(msg)
        if debug_container:
            debug_container.write(msg)
    
    debug(f"[{datetime.now()}] ランキング取得開始")
    
    config = load_config()
    products = load_products()
    
    debug(f"API Key設定: {'あり' if config.get('api_key') else 'なし'}")
    debug(f"商品数: {len(products)}")
    
    # エラーログをクリア
    clear_api_errors()
    
    if not config.get("api_key"):
        debug("❌ APIキーが未設定です")
        return []
    
    if not products:
        debug("❌ 商品リストが空です")
        return []
    
    df = load_data()
    all_results = []
    
    for product in products:
        asin = product.get('asin')
        if not asin:
            continue
        
        debug(f"📦 取得中: {asin}")
        result = fetch_ranking_for_product(config["api_key"], asin)
        
        if result:
            debug(f"  ✅ {result['title'][:30]}... ({len(result['results'])}カテゴリ)")
            all_results.extend(result['results'])
            update_product_title(asin, result['title'])
        else:
            debug(f"  ❌ 取得失敗: {asin}")
    
    # APIエラーを表示
    api_errors = get_api_errors()
    if api_errors:
        debug("--- APIエラー詳細 ---")
        for err in api_errors[:5]:  # 最初の5件だけ表示
            debug(f"⚠️ {err}")
    
    if all_results:
        save_ranking_data(all_results)
        send_slack_notification(config.get("slack_url"), all_results, df)
    
    debug(f"✅ 取得完了: {len(all_results)}件")
    return all_results

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
    
    # Supabase接続チェック
    supabase = get_supabase_client()
    if not supabase:
        st.error("⚠️ Supabaseの設定が必要です。Streamlit SecretsにSUPABASE_URLとSUPABASE_KEYを設定してください。")
        st.stop()
    
    config = load_config()
    products = load_products()
    df = load_data()
    
    # メトリクス
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📦 登録商品", len(products))
    col2.metric("📈 データ件数", len(df))
    col3.metric("💾 ストレージ", "Supabase")
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
                    st.error("⚠️ APIキーを設定してください（Streamlit Secrets）")
                elif not products:
                    st.error("⚠️ 商品を登録してください")
                else:
                    debug_container = st.container()
                    with st.spinner("Keepaからデータを取得中..."):
                        results = fetch_all_rankings(debug_container)
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
                        if save_product(asin):
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
                    if delete_product(p['asin']):
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
        st.subheader("⚙️ 設定情報")
        
        st.info("""
        **設定はStreamlit Secretsで管理されています**
        
        Streamlit Cloudのダッシュボード → Settings → Secrets で以下を設定してください：
        
        ```toml
        SUPABASE_URL = "https://xxxxx.supabase.co"
        SUPABASE_KEY = "eyJxxxx..."
        KEEPA_API_KEY = "あなたのKeepa APIキー"
        SLACK_WEBHOOK_URL = "https://hooks.slack.com/..."
        ```
        """)
        
        st.divider()
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
