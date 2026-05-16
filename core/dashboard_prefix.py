# -*- coding: utf-8 -*-
"""마운트 prefix 하에서 대시보드의 절대 /api 호출을 보정.

각 전략 대시보드(dashboard.html)는 절대경로 `/api/...` 로 백엔드를 호출한다.
sub-app 이 부모에 `/infinite` 등으로 마운트되면 그 호출이 부모 루트로 가서
404 가 된다. dashboard.html 을 수정하지 않고, sub-app 의 dashboard 라우트가
자신의 root_path(마운트 prefix)를 알아 fetch/XHR/EventSource 가 `/api`로
시작하는 요청만 prefix 를 붙이도록 하는 1회성 shim 을 주입한다.

단독 실행(미마운트) 시 root_path == "" → shim 은 즉시 return(무동작) →
원본 대시보드 동작과 완전히 동일(검증 가능).
"""
import json


def inject_api_base(html: str, base: str) -> str:
    base = base or ""
    b = json.dumps(base)
    shim = (
        "<script>(function(){var B=" + b + ";if(!B)return;"
        "var of=window.fetch;"
        "window.fetch=function(u,o){try{"
        "if(typeof u===\"string\"&&u.indexOf(\"/api\")===0){u=B+u;}"
        "else if(u&&u.url&&typeof u.url===\"string\"&&u.url.indexOf(\"/api\")===0){u=new Request(B+u.url,u);}"
        "}catch(e){}return of.apply(this,[u,o]);};"
        "var ox=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){try{"
        "if(typeof u===\"string\"&&u.indexOf(\"/api\")===0){arguments[1]=B+u;}"
        "}catch(e){}return ox.apply(this,arguments);};"
        "var OE=window.EventSource;if(OE){window.EventSource=function(u,c){try{"
        "if(typeof u===\"string\"&&u.indexOf(\"/api\")===0){u=B+u;}"
        "}catch(e){}return new OE(u,c);};}"
        "})();</script>"
    )
    low = html.lower()
    i = low.find("<head>")
    if i != -1:
        pos = i + len("<head>")
        return html[:pos] + shim + html[pos:]
    i = low.find("<head ")
    if i != -1:
        end = low.find(">", i)
        if end != -1:
            return html[:end + 1] + shim + html[end + 1:]
    i = low.find("<body")
    if i != -1:
        return html[:i] + shim + html[i:]
    return shim + html
