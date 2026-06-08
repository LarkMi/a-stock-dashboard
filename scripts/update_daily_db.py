"""
A股日线增量更新 (v3 - 直接写基表)
daily_adj是VIEW，底层是daily基表 + adj_factor表
"""
import sys, os, time, shutil
import duckdb, pandas as pd
import tushare as ts

DB_DIR = r'C:\Users\LarkMi\quant_20260525\每日A股日线行情数据duckDB（日更）'
# 最新DB用glob自动发现
import glob, os
_db_files = sorted(glob.glob(os.path.join(DB_DIR, 'daily_adj_*.duckdb')))
_db_files = [f for f in _db_files if os.path.getsize(f) > 100*1024*1024]
LATEST_DB = _db_files[-1] if _db_files else None

def main():
    pro = ts.pro_api()
    import glob
    files = sorted(glob.glob(os.path.join(DB_DIR, 'daily_adj_*.duckdb')))
    files = [f for f in files if os.path.getsize(f) > 100*1024*1024]
    db_path = files[-1]
    
    con = duckdb.connect(db_path)
    
    # 最新日期
    last = con.execute("SELECT MAX(trade_date) FROM daily_adj").fetchone()[0]
    print(f"📅 DB最新: {last}")
    # 计算需补充的交易日 (硬编码+推算, 避免trade_cal限流)
    from datetime import datetime, timedelta
    # 从last_date+1到今天的所有工作日（盘后15:30后当天数据已发布）
    last_dt = datetime.strptime(last, '%Y%m%d')
    end_dt = datetime.now()  # 当天（盘后数据已发布）
    all_dates = []
    d = last_dt + timedelta(days=1)
    while d <= end_dt:
        if d.weekday() < 5:  # 周一到周五
            all_dates.append(d.strftime('%Y%m%d'))
        d += timedelta(days=1)
    trade_dates = sorted(all_dates)
    print(f"需补 {len(trade_dates)} 天")
    if not trade_dates: con.close(); return
    
    total_new = 0
    for td in trade_dates:
        print(f"  {td}...", end=' ', flush=True)
        try:
            # ★ 直接写daily基表
            df = pro.daily(trade_date=td)
            if df is None or len(df) == 0:
                print("无数据"); continue
            
            # 只保留daily表需要的列
            daily_cols = ['ts_code','trade_date','open','high','low','close',
                          'pre_close','change','pct_chg','vol','amount']
            df_daily = df[daily_cols].copy()
            
            # 删除旧数据 + 插入新数据
            con.execute(f"DELETE FROM daily WHERE trade_date='{td}'")
            con.register('_df', df_daily)
            con.execute("INSERT INTO daily SELECT * FROM _df")
            
            # 更新adj_factor表 (限流处理)
            codes = df['ts_code'].tolist()
            batch = 300
            for i in range(0, len(codes), batch):
                try:
                    chunk = codes[i:i+batch]
                    adj = pro.adj_factor(ts_code=','.join(chunk), trade_date=td)
                    if adj is not None and len(adj) > 0:
                        adj_df = adj[['ts_code','trade_date','adj_factor']].copy()
                        con.execute(f"DELETE FROM adj_factor WHERE trade_date='{td}' AND ts_code IN ({','.join([chr(39)+c+chr(39) for c in chunk])})")
                        con.register('_adj', adj_df)
                        con.execute("INSERT INTO adj_factor SELECT * FROM _adj")
                except Exception as e:
                    if '频率超限' in str(e):
                        print(f'(adj限流,跳过)'); break
                time.sleep(0.35)
            
            n = len(df_daily)
            total_new += n
            print(f"✓ {n}条")
            
        except Exception as e:
            err = str(e)[:100]
            if '频率超限' in err:
                print(f"✗ daily限流,停止")
                break
            print(f"✗ {err}")
    
    con.close()
    
    # 保存新版本
    today = time.strftime('%Y%m%d')
    new_path = os.path.join(DB_DIR, f'daily_adj_19901219_{today}.duckdb')
    shutil.copy2(db_path, new_path)
    
    # 验证
    con = duckdb.connect(new_path, read_only=True)
    latest = con.execute("SELECT MAX(trade_date) FROM daily_adj").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM daily_adj").fetchone()[0]
    con.close()
    
    print(f"\n✅ {new_path}")
    print(f"   新增约{total_new}行 | 总计{total:,}行 | 最新{latest}")

if __name__ == '__main__':
    main()
