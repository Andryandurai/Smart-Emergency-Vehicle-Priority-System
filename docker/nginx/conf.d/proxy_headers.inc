# Headers every proxied location needs.
#
# X-Forwarded-Proto is the one that matters most: Django's
# SECURE_PROXY_SSL_HEADER reads it to decide whether the request arrived over
# TLS. Without it, a production deployment behind TLS termination sees every
# request as plain HTTP, SECURE_SSL_REDIRECT loops, and the secure cookie flags
# behave as though the site were insecure.
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_redirect off;
