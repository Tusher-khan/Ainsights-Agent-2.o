import sys, time, json
from playwright.sync_api import sync_playwright
import b3
B=b3.B
def run(w,h):
    with sync_playwright() as p:
        br=p.chromium.launch(executable_path='/opt/pw-browsers/chromium', args=['--no-sandbox'])
        ctx=br.new_context(viewport={'width':w,'height':h}, bypass_csp=True)
        ctx.route('**/cdn.plot.ly/**', lambda r: r.fulfill(status=200, content_type='application/javascript', body=b3.STUB))
        ctx.route(lambda u: any(x in u for x in ('fonts.googleapis','fonts.gstatic','cdn.jsdelivr')), lambda r: r.abort())
        pg=ctx.new_page(); errs=[]; pg.on('pageerror',lambda e:errs.append(str(e)))
        pg.goto(B+'/'); pg.wait_for_timeout(600)
        print(w,'landing url',pg.url)
        pg.click('.lp-hero a.btn-grad'); pg.wait_for_url('**/register'); print('CTA ->',pg.url)
        pg.go_back(); pg.click('.lp-hero >> text=See How It Works'); pg.wait_for_timeout(500); print('hash',pg.url.split('#')[-1], 'scrollY>0', pg.evaluate('scrollY')>0)
        pg.goto(B+'/register'); email=f'flow{int(time.time()*1000)}@example.com'
        pg.fill('#name','Flow User'); pg.fill('#email',email); pg.fill('#password','correct-horse-battery'); pg.click('button[type=submit]'); pg.wait_for_url('**/dashboard',timeout=15000)
        pg.wait_for_selector('.onboarding'); print('onboarding cards',pg.eval_on_selector_all('.ob-card','e=>e.length'),'welcome-band hidden',pg.evaluate("document.querySelector('.welcome-band').hidden"))
        print('hoverflow',pg.evaluate("document.documentElement.scrollWidth>innerWidth"))
        pg.screenshot(path=f'onb_{w}.png')
        pg.click('[data-sample]'); pg.wait_for_url('**/chat?dataset=*',timeout=30000); pg.wait_for_timeout(1200)
        print('chat dataset select',pg.eval_on_selector('#dataset-select','e=>e.selectedOptions[0].textContent'))
        txt,plots,err=b3.ask(pg,'What are my total sales?'); print('Q1',txt[:110].replace('\n',' '),err)
        print('next chips',pg.eval_on_selector_all('.record:last-of-type .next-chip','e=>e.map(x=>x.textContent)'))
        txt,plots,err=b3.ask(pg,'Which region performs best?'); print('Q2',[l for l in txt.split('\n') if l.strip()][4:8],err)
        n=len(pg.query_selector_all('.record')); pg.click('.record:last-of-type .next-chip >> nth=0')
        pg.wait_for_function(f"document.querySelectorAll('.record').length>{n} && !document.querySelector('.progress-card')",timeout=60000)
        print('chip click ->',pg.inner_text('.turn-question >> nth=-1'))
        print('hoverflow',pg.evaluate("document.documentElement.scrollWidth>innerWidth"),'errors',errs)
        # empty states for a fresh account
        for u in ('/history','/dashboards'):
            pass
        br.close()
run(int(sys.argv[1]),int(sys.argv[2]))
