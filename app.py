import streamlit as st
import pandas as pd
from datetime import datetime
import pytz
import io

st.set_page_config(page_title="SqOff Performance Analysis", layout="wide")

st.title("📊 SqOff Performance Analyzer")

# --- File Uploads ---
mtm_file = st.file_uploader("Upload Saved MTM File (.xlsx)", type=["xlsx"])
stoploss_file = st.file_uploader("Upload STOPLOSS SENT File (.csv)", type=["csv"])
sqoff_file = st.file_uploader("Upload SqOff Performance File (.xlsx)", type=["xlsx"])

if mtm_file and stoploss_file and sqoff_file:
    # --- Load sqoff_performance (sheet1) ---
    sqoff_df = pd.read_excel(sqoff_file, sheet_name=0, dtype={'user_id': str})
    sqoff_df['user_id'] = sqoff_df['user_id'].astype(str).str.strip()
    sqoff_df['order_side'] = sqoff_df['order_side'].astype(str).str.strip().str.upper()
    sqoff_df['order_status'] = sqoff_df['order_status'].astype(str).str.strip().str.title()
    sqoff_df['order_fire_time'] = pd.to_datetime(
        sqoff_df['order_fire_time'], format='%H:%M:%S.%f', errors='coerce'
    )

    # Min / Max order_fire_time per user
    min_max_order_times = (
        sqoff_df.groupby('user_id')['order_fire_time']
        .agg(['min', 'max'])
        .dropna()
        .reset_index()
    )
    min_max_order_times['min_order_fire_time'] = min_max_order_times['min'].dt.strftime('%H:%M:%S.%f').str[:-3]
    min_max_order_times['max_order_fire_time'] = min_max_order_times['max'].dt.strftime('%H:%M:%S.%f').str[:-3]
    min_order_times = min_max_order_times[['user_id', 'min_order_fire_time', 'max_order_fire_time']]

    # --- Performance calculation ---
    results = []
    for user_id, group in sqoff_df.groupby('user_id'):
        buy_orders = group[group['order_side'] == 'BUY']
        total_buy = len(buy_orders)
        completed_buy = len(buy_orders[buy_orders['order_status'].isin(['Filled', 'Complete'])])
        rejected_buy = len(buy_orders[buy_orders['order_status'] == 'Rejected'])
        performance_ratio = completed_buy / total_buy if total_buy > 0 else 0
        critical_exchange = len(buy_orders[
            (buy_orders['order_status'].isin(['Filled', 'Complete'])) &
            (buy_orders['exchange_response_ms'] > 1000)
        ])
        critical_order = len(buy_orders[
            (buy_orders['order_status'].isin(['Filled', 'Complete'])) &
            (buy_orders['order_response_ms'] > 1200)
        ])
        results.append({
            'user_id': user_id,
            'total_buy_orders': total_buy,
            'completed_buy_orders': completed_buy,
            'rejected_buy_orders': rejected_buy,
            'performance_ratio': performance_ratio,
            'critical_exchange_ms (>1000ms)': critical_exchange,
            'critical_order_ms (>1200ms)': critical_order
        })
    new_df = pd.DataFrame(results)

    # --- Users sheet ---
    users_df = pd.read_excel(sqoff_file, sheet_name="users", dtype={'userId': str})
    users_df.rename(columns={'userId': 'user_id'}, inplace=True)
    users_df['sl_broker'] = users_df['max_loss'] / users_df['broker_sl']
    users_df['should_be_rms'] = users_df['sl_broker'].apply(lambda x: 'MSTECH' if x < 1.09 else 'RMS')
    users_df['sqoff_initiated_time'] = pd.to_datetime(
        users_df['sqoff_initiated_time'], errors='coerce'
    ).dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata').dt.strftime('%Y-%m-%d %H:%M:%S.%f')

    # --- MTM File ---
    mtm_df = pd.read_excel(mtm_file, dtype={'user_id': str})
    mtm_df['MTM%'] = (mtm_df['MTM'] / mtm_df['allocation'] * 100).round(3)

    # --- Stoploss file ---
    df_raw = pd.read_csv(stoploss_file, header=None)
    rows = []
    for _, row in df_raw.iterrows():
        timestamp_str = str(row[0]).strip()
        user_data = str(row[1]).strip()
        if timestamp_str.isdigit():
            dt = datetime.fromtimestamp(int(timestamp_str) / 1000.0, tz=pytz.UTC)
            timestamp = dt.astimezone(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            user_ids = [uid.strip() for uid in user_data.split(',') if uid.strip()]
            for uid in user_ids:
                rows.append([timestamp, uid])
    df_melted = pd.DataFrame(rows, columns=["stoploss_sent", "user_id"])
    df_melted["user_id"] = df_melted["user_id"].astype(str)

    # --- Merge everything ---
    merged_result = (
        mtm_df
        .merge(new_df, on='user_id', how='left')
        .merge(users_df, on='user_id', how='left')
        .merge(df_melted, on='user_id', how='left')
        .merge(min_order_times, on='user_id', how='left')
    )

    merged_result['triggered_stoploss_sent'] = ~merged_result['stoploss_sent'].isna()

    merged_result['stoploss_sent_dt'] = pd.to_datetime(merged_result['stoploss_sent'], errors='coerce')
    merged_result['sqoff_initiated_time_dt'] = pd.to_datetime(merged_result['sqoff_initiated_time'], errors='coerce')

    merged_result['sqoff_stoploss_diff_ms'] = (
        (merged_result['stoploss_sent_dt'] - merged_result['sqoff_initiated_time_dt'])
        .dt.total_seconds() * 1000
    )

    merged_result['critical'] = (
        ((merged_result['sqoff_stoploss_diff_ms'] > 200)) |
        (merged_result['MTM%'] < -1.3) |
        ((merged_result['critical_order_ms (>1200ms)'].fillna(0) > 0) &
         (merged_result['critical_exchange_ms (>1000ms)'].fillna(0) > 0)) |
        (merged_result['total_buy_orders'].fillna(0) == 0) |
        (merged_result['completed_buy_orders'].fillna(0) == 0) |
        (merged_result['performance_ratio'].fillna(1) < 0.10) |
        (merged_result['triggered_stoploss_sent'] != True)
    )
    merged_result['critical'] = merged_result['critical'].fillna(False)

    # --- Final Selection ---
    final_columns = [
        'critical', 'user_id', 'alias', 'broker', 'MTM', 'MTM%', 'algo_x', 'server',
        'allocation_x', 'capital_x', 'max_loss_x', 'total_buy_orders', 'completed_buy_orders',
        'rejected_buy_orders', 'performance_ratio', 'critical_exchange_ms (>1000ms)',
        'critical_order_ms (>1200ms)', 'sqoff_initiated', 'sqoff_initiated_time', 'broker_sl',
        'should_be_rms', 'sl_broker', 'stoploss_sent', 'min_order_fire_time','max_order_fire_time',
        'triggered_stoploss_sent', 'sqoff_stoploss_diff_ms', 'org', 'realizedMTM',
        'unRealizedMTM', 'date'
    ]
    final_df = merged_result[final_columns]



        # --- Combined Summary: Critical AND SqOff Initiated ---
    combined_df = final_df[
        (final_df['critical'] == True) & 
        (final_df['sqoff_initiated'] == True)
    ]

    st.subheader("🔴  Summary (critical = True AND sqoff_initiated = True)")

    total_users = combined_df['user_id'].nunique()

    st.markdown(f"**🧾 Total Users :** {total_users}")

    if combined_df.empty:
        st.success("No users found with both critical = True AND sqoff_initiated = True ✅")
    else:
        st.dataframe(combined_df, use_container_width=True)

        # Download combined data
        buffer_comb = io.BytesIO()
        combined_df.to_excel(buffer_comb, index=False)
        buffer_comb.seek(0)
        st.download_button("⬇️ Download ", data=buffer_comb, file_name="critical_and_sqoff_initiated.xlsx", mime="application/vnd.ms-excel")
        # st.download_button("⬇️ Download Combined CSV", data=combined_df.to_csv(index=False), file_name="critical_and_sqoff_initiated.csv", mime="text/csv")


    # --- Show Final Data ---
    st.subheader("✅ Final Analysis Output")
    st.dataframe(final_df, use_container_width=True)

    # Download full dataset
    buffer = io.BytesIO()
    final_df.to_excel(buffer, index=False)
    buffer.seek(0)
    st.download_button("⬇️ Download", data=buffer, file_name="final_sqoff_analysis.xlsx", mime="application/vnd.ms-excel")
    # st.download_button("⬇️ Download CSV", data=final_df.to_csv(index=False), file_name="final_sqoff_analysis.csv", mime="text/csv")

    # --- Combined Summary: Critical AND SqOff Initiated ---
   