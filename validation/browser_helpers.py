import json, sys
from playwright.sync_api import sync_playwright
V='/tmp/claude-0/val'; B='http://127.0.0.1:5055'
STUB="""window.__plots=[];window.Plotly={newPlot:(id,d,l,c)=>{window.__plots.push({id,data:d,layout:l});return Promise.resolve()},react:(id,d,l,c)=>{window.__plots.push({id,data:d,layout:l});return Promise.resolve()},Plots:{resize(){}},purge(){},relayout(){}};"""
def mk(p, w=1366, h=1000):
    br=p.chromium.launch(executable_path='/opt/pw-browsers/chromium', args=['--no-sandbox'])
    ctx=br.new_context(viewport={'width':w,'height':h}, bypass_csp=True, storage_state=f'{V}/state.json')
    ctx.route('**/cdn.plot.ly/**', lambda r: r.fulfill(status=200, content_type='application/javascript', body=STUB))
    ctx.route(lambda u: any(x in u for x in ('fonts.googleapis','fonts.gstatic','cdn.jsdelivr')), lambda r: r.abort())
    return br, ctx
def ask(pg, q):
    n=len(pg.query_selector_all('.record'))
    pg.fill('#question', q); pg.keyboard.press('Enter')
    pg.wait_for_function(f"document.querySelectorAll('.record').length>{n} && !document.querySelector('.progress-card')", timeout=90000); pg.wait_for_timeout(500)
    rec=pg.query_selector_all('.record')[-1]
    return rec.inner_text(), pg.evaluate('window.__plots.splice(0)'), 'is-error' in (rec.get_attribute('class') or '')
if __name__=='__main__':
    with sync_playwright() as p:
        br,ctx=mk(p); pg=ctx.new_page(); errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.goto(B+'/chat'); pg.wait_for_timeout(1200)
        ds=pg.eval_on_selector_all('#dataset-select option','o=>o.map(x=>[x.value,x.textContent])')
        xl=[v for v,t in ds if t.startswith('orders')][0]; pg.select_option('#dataset-select', xl)
        for q in ["What was revenue from the Mars region?","Show average Profit by Foo","Why did our sales decrease last month?","Show the top 5 products in South","What about East?","only the top 3","as a pie chart","Which region had the lowest sales?"]:
            t,pl,err=ask(pg,q); print('='*90); print('Q:',q,'| ERROR-CARD' if err else ''); print(t[:700].replace('\n',' | ')); print('PLOTS',[(x['data'][0].get('type'),x['layout'].get('title',{}).get('text','')[:60]) for x in pl])
            pg.screenshot(path=f'{V}/f_{q[:12].replace(" ","_")}.png')
        print('page errors',errs); br.close()
