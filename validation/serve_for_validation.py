import os, sys, importlib.util
from pathlib import Path
root = Path("/home/claude/project_audit/repo/ai_analytics_chatbot"); sys.path.insert(0, str(root))
base = Path("/tmp/claude-0/val/state"); base.mkdir(exist_ok=True)
os.environ.update(DATABASE_URL=f"sqlite:///{base}/v.db", ANALYTICS_DATABASE_URL=f"sqlite:///{base}/v.db",
  CHROMA_PATH=str(base/"chroma"), UPLOAD_DIR=str(base/"uploads"), AUTH_REQUIRED="true", DEMO_MODE="true",
  CONNECTOR_ALLOW_PRIVATE_NETWORK="true", CONNECTOR_ALLOW_INSECURE_HTTP="true", SECRET_KEY="validation-secret-key-123456789", EMAIL_LOG_LINKS="true")
spec = importlib.util.spec_from_file_location("entry", root/"app.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.create_app().run(host="127.0.0.1", port=5055, threaded=True, use_reloader=False)
