"""
WSGI config for observatory project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'observatory.settings')
# Web requests only: no page should hold a query open for longer than a minute.
# manage.py never imports this module, so the analysis commands are unaffected.
os.environ.setdefault('GTFSOBS_STATEMENT_TIMEOUT_MS', '60000')

application = get_wsgi_application()
