import json, pandas as pd
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

class H(BaseHTTPRequestHandler):
    def log_message(s,*a): pass
    def do_GET(s):
        if s.headers.get('Authorization')!='Bearer restkey123': s.send_response(401); s.end_headers(); return
        rows=json.load(open('/tmp/claude-0/val/rest_rows.json')); q=parse_qs(urlparse(s.path).query); p=int(q.get('page',['1'])[0]); n=500
        body=json.dumps({"data":{"items":rows[(p-1)*n:p*n]}}).encode()
        s.send_response(200); s.send_header('Content-Type','application/json'); s.end_headers(); s.wfile.write(body)
HTTPServer(('127.0.0.1',5056),H).serve_forever()
