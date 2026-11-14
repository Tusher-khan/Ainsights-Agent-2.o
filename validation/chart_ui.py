import json
from playwright.sync_api import sync_playwright
import b3
DS='ds_7fa20032cdeb'
def run(w,h):
    with sync_playwright() as p:
        br,ctx=b3.mk(p,w,h); pg=ctx.new_page(); errs=[]; pg.on('pageerror',lambda e:errs.append(str(e)))
        reqs=[]; pg.on('request',lambda r: reqs.append(r) if '/api/chat/async' in r.url and r.method=='POST' else None)
        pg.goto(b3.B+'/chat'); pg.wait_for_timeout(1000); pg.select_option('#dataset-select',DS)
        txt,plots,err=b3.ask(pg,'Which region performs best?'); print(w,'answer:',txt[:70].replace('\n',' '),'plots',len(plots),'err',err)
        tools=pg.eval_on_selector_all('.record .chart-tool','e=>e.map(x=>x.dataset.chartAction)'); print('tools',tools)
        # show data inline
        pg.click('.record [data-chart-action="data"]'); print('inline data rows',pg.eval_on_selector_all('.chart-data tbody tr','e=>e.length'))
        pg.click('.record [data-chart-action="data"]')
        # expand
        pg.click('.record [data-chart-action="expand"]'); pg.wait_for_timeout(300)
        print('modal open',pg.is_visible('.chart-modal'),'title',pg.inner_text('#chart-modal-title'),'| side:',pg.inner_text('.chart-modal-side')[:120].replace('\n',' / '))
        print('modal plot present',pg.evaluate("window.__plots.filter(p=>p.id==='chart-modal-plot').length"))
        pg.click('[data-modal-data]'); print('modal data rows',pg.eval_on_selector_all('.chart-modal-data tbody tr','e=>e.length'))
        pg.fill('.chart-modal-data .dt-search','West'); print('search rows',pg.eval_on_selector_all('.chart-modal-data tbody tr','e=>e.length'))
        pg.fill('.chart-modal-data .dt-search','')
        pg.click('.chart-modal-data th[data-sort]'); print('sorted',pg.get_attribute('.chart-modal-data th[data-sort]','aria-sort'))
        pg.click('.chart-modal [data-chart-action="png"]'); print('png call',pg.evaluate("window.__calls.filter(c=>c[0]==='downloadImage').length"))
        pg.click('.chart-modal [data-chart-action="zoom"]'); pg.click('.chart-modal [data-chart-action="reset"]'); print('zoom calls',pg.evaluate("window.__calls.filter(c=>c[0]==='relayout').map(c=>JSON.stringify(c[1]))"))
        with pg.expect_download() as d: pg.click('.chart-modal [data-chart-action="csv"]')
        print('csv download',d.value.suggested_filename)
        pg.keyboard.press('Escape'); print('modal closed',not pg.is_visible('.chart-modal'))
        # drill-down
        pg.evaluate("""()=>{const el=document.querySelector('.record .record-chart');el.__emit('plotly_click',{points:[{x:'West',y:1,data:{orientation:'v'}}],event:{clientX:400,clientY:300}})}""")
        pg.wait_for_selector('.drill-q',timeout=5000); print('drill options',pg.eval_on_selector_all('.drill-q','e=>e.map(x=>x.textContent)'))
        pg.evaluate("""()=>{const el=document.querySelector('.record .record-chart');}""")
        n=len(pg.query_selector_all('.record'))
        pg.click('.drill-q >> nth=0'); pg.wait_for_function(f"document.querySelectorAll('.record').length>{n} && !document.querySelector('.progress-card')",timeout=60000)
        body=json.loads(reqs[-1].post_data); print('drill request chart_context',body.get('chart_context'),'q=',body['question'])
        print('drill answer',pg.inner_text('.record:last-of-type')[:100].replace('\n',' '))
        # ask about chart
        pg.click('.record >> nth=0 >> [data-chart-action="ask"]'); print('chip',pg.inner_text('#chart-chip')[:60])
        n=len(pg.query_selector_all('.record')); pg.fill('#question','Why is West ahead?'); pg.keyboard.press('Enter')
        pg.wait_for_function(f"document.querySelectorAll('.record').length>{n} && !document.querySelector('.progress-card')",timeout=60000)
        print('ask req ctx',json.loads(reqs[-1].post_data).get('chart_context'),'chip cleared',pg.inner_text('#chart-chip')=='' )
        print('answer:',pg.inner_text('.record:last-of-type')[:120].replace('\n',' '))
        m=pg.evaluate("()=>[document.documentElement.scrollWidth>innerWidth, document.querySelector('.sidebar').getBoundingClientRect().top]"); print('hoverflow/sidebar',m)
        pg.screenshot(path=f'chart_{w}.png'); print('errors',errs); br.close()
import sys
run(int(sys.argv[1]),int(sys.argv[2]))
