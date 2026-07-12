#!/bin/bash
# Single URL checker - called by xargs with args: idx url
idx="$1"
url="$2"
results_dir="/tmp/urlcheck_results"

result=$(curl -o /dev/null -s -w "%{http_code}||%{url_effective}||%{redirect_count}" \
    --connect-timeout 15 --max-time 30 \
    -x http://127.0.0.1:7890 \
    -L --max-redirs 5 \
    "$url" 2>&1)
curl_exit=$?

if [ $curl_exit -ne 0 ]; then
    error_msg=""
    if echo "$result" | grep -qi "timeout\|timed out\|Connection timed out"; then
        error_msg="TIMEOUT"
    elif echo "$result" | grep -qi "could not resolve\|Could not resolve host\|Name or service not known\|Temporary failure in name resolution"; then
        error_msg="DNS_FAILURE"
    elif echo "$result" | grep -qi "SSL certificate\|certificate verify failed\|SSL connection"; then
        error_msg="SSL_ERROR"
    elif echo "$result" | grep -qi "Connection refused\|connection refused"; then
        error_msg="CONNECTION_REFUSED"
    elif echo "$result" | grep -qi "No route to host"; then
        error_msg="NO_ROUTE"
    else
        error_msg="CURL_ERROR($curl_exit)"
    fi
    echo "$url||ERROR||$error_msg||$result" > "$results_dir/result_${idx}.txt"
    exit 0
fi

IFS='||' read -r http_code final_url redirect_count <<< "$result"

problem=""
if [ "$http_code" = "404" ]; then
    problem="404_NOT_FOUND"
elif [ "$http_code" = "410" ]; then
    problem="410_GONE"
elif [ "$http_code" = "403" ]; then
    problem="403_FORBIDDEN"
elif [ "${http_code:0:1}" = "5" ]; then
    problem="5XX_SERVER_ERROR"
elif [ "$http_code" = "000" ]; then
    problem="NO_RESPONSE"
else
    problem="OK"
fi

echo "$url||$final_url||$http_code||$redirect_count||$problem" > "$results_dir/result_${idx}.txt"
