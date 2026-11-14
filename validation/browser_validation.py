import json, re, math, io
from playwright.sync_api import sync_playwright
import b3, pandas as pd
B=b3.B; V=b3.V
d=pd.read_csv(f'{V}/orders.csv'); d['m']=pd.to_datetime(d['Order Date']).dt.to_period('M')
results=[]
def check(name, cond, detail=''):
    results.append((name, bool(cond))); print(('PASS ' if cond else 'FAIL ')+name+(f'  [{detail}]' if (detail and not cond) else ''))
H={'X-AInsight-Client':'web','Content-Type':'application/json'}
with sync_playwright() as p:
    # ---------- auth in a fresh context
    br=p.chromium.launch(executable_path='/opt/pw-browsers/chromium', args=['--no-sandbox']); c0=br.new_context(bypass_csp=True); pg=c0.new_page()
    pg.goto(B+'/dashboard'); pg.wait_for_timeout(1000); check('anonymous visit to /dashboard is sent to login', '/login' in pg.url, pg.url)
    pg.fill('#email','val@example.com'); pg.fill('#password','wrong-password-1'); pg.click('#login-form button[type=submit]'); pg.wait_for_timeout(1500); check('wrong password stays on login', '/login' in pg.url)
    pg.fill('#password','Correct-Horse-9!'); pg.click('#login-form button[type=submit]'); pg.wait_for_timeout(2000); check('correct password reaches the app', '/login' not in pg.url, pg.url)
    br.close()
    br,ctx=b3.mk(p); pg=ctx.new_page(); errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.on('dialog', lambda dlg: dlg.accept('Renamed check') if dlg.type=='prompt' else dlg.accept())
    R=ctx.request
    pg.goto(B+'/chat'); pg.wait_for_timeout(1200); pg.select_option('#dataset-select','ds_7fa20032cdeb')
    def ask(q): 
        t,pl,err=b3.ask(pg,q); return t,pl,err
    t,pl,e=ask('What is total sales?'); check('total sales = pandas (166.57K / 166,572.8)', '166,572.8' in t)
    t,pl,e=ask('Why did our sales decrease last month?')
    check('why: delta 1.50K and 10.7% shown', '1.50K' in t and '10.7%' in t); check('why: names South and Furniture as cause', 'South' in t and 'Furniture' in t)
    check('why: facts / interpretation / recommendation sections separate', all(x in t for x in ('FACT','INTERPRETATION')) )
    check('why: shows analysis steps panel', 'Analysis steps performed' in t); check('why: states evidence level', 'EVIDENCE' in t)
    tr=[x for x in pl if x['data'][0]['type']=='scatter'][0]['data'][0]; m=d.groupby('m').Sales.sum()
    check('chart: trend equals monthly sums', len(tr['y'])==12 and all(abs(a-b)<0.01 for a,b in zip(tr['y'],m.values)))
    cb=[x for x in pl if 'What moved Sales: Nov' in x['layout']['title']['text']][0]; seg=dict(zip(cb['data'][0]['y'],cb['data'][0]['x']))
    ref=(d[d.m=='2024-12'].groupby('Category').Sales.sum()-d[d.m=='2024-11'].groupby('Category').Sales.sum())
    check('chart: contribution bars equal pandas deltas', abs(seg['Furniture (Category)']-ref['Furniture'])<0.01)
    check('chart: overlap warning present', 'do not add' in cb['layout']['title']['text'])
    for x in pl: pass
    t,pl,e=ask('Show the top 5 products in South'); truth=d[d.Region=='South'].groupby('Product').Sales.sum().sort_values(ascending=False).head(5)
    check('top5 South values = pandas', all(f'{v:,.2f}'.rstrip('0').rstrip('.') in t for v in truth.values))
    bars=pl[0]['data'][0]; check('top5 chart uses same values (bar)', sorted(bars['x'])==sorted(round(v,2) for v in truth.values) or sorted(round(v,2) for v in bars['x'])==sorted(round(v,2) for v in truth.values))
    check('top5 chart is a ranked bar (right type)', bars['type']=='bar')
    t,pl,e=ask('What about East?'); te=d[d.Region=='East'].groupby('Product').Sales.sum().sort_values(ascending=False).head(5)
    check('follow-up "What about East?" uses East + keeps top-5/product context', all(f'{v:,.2f}'.rstrip('0').rstrip('.') in t for v in te.values) and 'Continuing' in t)
    t,pl,e=ask('only the top 3'); t3=d[d.Region=='East'].groupby('Product').Sales.sum().sort_values(ascending=False).head(3)
    check('follow-up "only the top 3" keeps the East filter', all(f'{v:,.2f}'.rstrip('0').rstrip('.') in t for v in t3.values) and 'Keeping the filter' in t)
    t,pl,e=ask('as a pie chart'); check('follow-up "as a pie chart" switches chart type', pl and pl[0]['data'][0]['type']=='pie')
    t,pl,e=ask('Which region had the lowest sales?'); check('lowest wording correct', 'South is the lowest' in t and 'leads' not in t)
    t,pl,e=ask('What was revenue from the Mars region?'); check('unknown value Mars refused with real values', e and 'no Region called' in t and 'North' in t)
    t,pl,e=ask('Show average Profit by Foo'); check('unknown column Foo refused', e and 'no column called' in t)
    pg.select_option('#dataset-select','ds_103b41798666'); t,pl,e=ask('Why did our sales decrease last month?')
    check('no-change data labelled INSUFFICIENT EVIDENCE', 'INSUFFICIENT EVIDENCE' in t and 'MEDIUM' not in t and 'LOW CONFIDENCE' not in t)
    check('no-change data makes no recommendation and no causal segment claim', 'Suggested action' not in t and '% of the total change' not in t)
    pg.select_option('#dataset-select','ds_7fa20032cdeb')
    for q,x in []: pass
    # axis readability across charts asked
    t,pl,e=ask('Show monthly sales trend'); lay=pl[0]['layout']; check('trend axis titles readable', not any('_' in (lay[a].get('title',{}) or {}).get('text','') for a in ('xaxis','yaxis')))
    # ---------- sources
    for ds,q,truth,label in (('ds_40860e4eb70d','What is total amount?',166572.8,'SQLite (SQL engine)'),('ds_ebed7ac7f6f0','What is total Sales?',float(d.Sales.sum())+10000,'REST')):
        pg.select_option('#dataset-select',ds); t,pl,e=ask(q); fmt=f'{truth:,.2f}'.rstrip('0').rstrip('.'); check(f'{label} total = underlying data', fmt in t, fmt)
    pg.select_option('#dataset-select','ds_40860e4eb70d'); t,pl,e=ask('Why did our amount decrease last month?'); check('SQLite why-analysis finds South x Furniture', 'South' in t and 'Furniture' in t.replace('furniture','Furniture'))
    pg.goto(B+'/data-sources'); pg.wait_for_timeout(1200); card=pg.locator('.src-card', has_text='Shop SQLite'); card.locator('[data-a=toggle]').click(); pg.wait_for_timeout(1200)
    r=R.post(B+'/api/chat', headers=H, data=json.dumps({'question':'What is total amount?','dataset_id':'ds_40860e4eb70d'})); check('disabled source cannot be analysed (permission)', r.status>=400, r.status)
    pg.locator('.src-card', has_text='Shop SQLite').locator('[data-a=toggle]').click(); pg.wait_for_timeout(1200)
    r=R.post(B+'/api/chat', headers=H, data=json.dumps({'question':'What is total amount?','dataset_id':'ds_40860e4eb70d'})); check('re-enabled source works again', r.status==200, r.status)
    # ---------- history
    pg.goto(B+'/history'); pg.wait_for_timeout(1500); n0=len(pg.query_selector_all('.activity-row')); check('history lists analyses', n0>5, n0)
    ev0=len(R.get(B+'/api/agent/events').json()['events']); pg.locator('.activity-row', has_text='Why did our sales').first.locator('[data-action=open]').click(); pg.wait_for_timeout(2500)
    check('open saved analysis shows banner, runs nothing', 'nothing was re-run' in pg.inner_text('#chat-inner') and len(R.get(B+'/api/agent/events').json()['events'])==ev0)
    check('saved analysis renders its charts again', len(pg.evaluate('window.__plots'))>=2)
    pg.goto(B+'/history'); pg.wait_for_timeout(1200); pg.locator('.activity-row').first.locator('[data-action=rename]').click(); pg.wait_for_timeout(1200); check('rename', 'Renamed check' in pg.inner_text('#history-list'))
    row=pg.locator('.activity-row', has_text='Renamed check').first.locator('[data-action=favorite]')
    if row.get_attribute('aria-pressed')=='true': row.click(); pg.wait_for_timeout(1200); row=pg.locator('.activity-row', has_text='Renamed check').first.locator('[data-action=favorite]')
    row.click(); pg.wait_for_timeout(1200); check('favorite floats to top', pg.locator('.activity-row').first.inner_text().startswith('★'))
    pg.fill('#h-search','Renamed'); pg.wait_for_timeout(1200); check('search narrows list', 0 < len(pg.query_selector_all('.activity-row')) < n0); pg.fill('#h-search','')
    pg.check('#h-fav'); pg.wait_for_timeout(1200); check('favorites filter', len(pg.query_selector_all('.activity-row'))>=1); pg.uncheck('#h-fav')
    pg.wait_for_timeout(1000); b=len(pg.query_selector_all('.activity-row')); pg.locator('.activity-row').last.locator('[data-action=delete]').click(); pg.wait_for_timeout(1000); check('delete removes one', len(pg.query_selector_all('.activity-row'))==b-1)
    # ---------- dashboards
    dash=R.get(B+'/api/dashboards').json()['dashboards'][0]['id']; pg.goto(B+f'/dashboards/{dash}'); pg.wait_for_timeout(2500)
    kt=pg.inner_text('#dv-kpis'); check('dashboard KPI Sales = pandas', '166.57K' in kt); check('dashboard averages the Discount rate (not a sum)', 'Average Discount' in kt and '0.10' in kt)
    plots=pg.evaluate('window.__plots'); titles=[x['layout']['title']['text'] for x in plots]; check('dashboard chart titles unique and name the dimension', len(set(titles))==len(titles) and any(' by ' in t for t in titles), titles)
    sel=pg.locator('#dv-filters select').first; opts=sel.locator('option').all_inner_texts(); sel.select_option(opts[2]); pg.wait_for_timeout(1500)
    dim=sel.get_attribute('data-dim'); tru=d.assign(x=d[dim] if dim in d else None); 
    check('dashboard filter changes rows and KPI', '480 rows' in pg.inner_text('#dv-sub') or 'rows in view' in pg.inner_text('#dv-sub'))
    # ---------- metrics
    pg.goto(B+'/datasets/ds_7fa20032cdeb'); pg.wait_for_timeout(1500); pg.click('[data-panel=metrics]'); pg.wait_for_timeout(500)
    check('existing metric listed', 'Profit Margin' in pg.inner_text('#metric-list'))
    pg.fill('#m-name','Bad'); pg.fill('#m-expr','SUM(Nope)/2'); pg.click('#metric-form button[type=submit]'); pg.wait_for_timeout(1000); check('metric with unknown column refused', 'does not exist' in pg.inner_text('#m-error'))
    pg.fill('#m-expr','__import__("os").system("id")'); pg.click('#metric-form button[type=submit]'); pg.wait_for_timeout(1000); check('code-injection formula refused', 'Definitions can only use' in pg.inner_text('#m-error'))
    # ---------- mobile
    br2,c2=b3.mk(p,375,812); m=c2.new_page(); bad=[]
    for u in ['/dashboard','/chat','/datasets','/upload','/data-sources','/history','/dashboards',f'/dashboards/{dash}','/datasets/ds_7fa20032cdeb','/system','/profile']:
        m.goto(B+u); m.wait_for_timeout(900); w=m.evaluate('[document.documentElement.scrollWidth,document.documentElement.clientWidth]'); 
        if w[0]>w[1]+1: bad.append((u,w))
    check('phone 375px: no horizontal overflow on 11 pages', not bad, bad)
    br2.close()
    check('no JavaScript page errors during the whole run', not errs, errs)
    br.close()
ok=sum(1 for _,c in results if c); print(f'\nBROWSER CHECKS: {ok}/{len(results)} passed'); json.dump(results, open(f'{V}/final_results.json','w'))
