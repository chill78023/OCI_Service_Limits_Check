"""OCI utilization dashboard. Cloud credentials come only from resource identity."""
import base64
import concurrent.futures
import datetime as dt
import io
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import oci
from fdk import response

LOG = logging.getLogger('limits-dashboard')
REGIONS = ('us-ashburn-1', 'us-chicago-1', 'us-phoenix-1', 'eu-frankfurt-1')
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def cloud_client(cls, region=None, signer=None):
    signer = signer or oci.auth.signers.get_resource_principals_signer()
    return cls({'region': region or os.environ.get('OCI_RESOURCE_PRINCIPAL_REGION', 'us-ashburn-1')},
               signer=signer, timeout=(10, 30), retry_strategy=oci.retry.NoneRetryStrategy())

def pages(method, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data

def utilization(limit, used):
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None, False
    value = int(limit)
    if value <= 0:
        return None, used > 0
    return used / value, used >= value * .8

def safe_limit(value):
    # JavaScript and Excel cannot exactly represent large signed 64-bit values.
    return str(value) if abs(value) >= 10**15 else value

def collect_snapshot():
    started = now()
    tenancy = os.environ['TENANCY_ID']
    signer = oci.auth.signers.get_resource_principals_signer()
    rows = []
    def collect_region(region):
        client = cloud_client(oci.limits.LimitsClient, region, signer)
        services = pages(client.list_services, tenancy)
        definitions = {(d.service_name, d.name): d for d in pages(client.list_limit_definitions, tenancy)}
        def service_limits(service):
            c = cloud_client(oci.limits.LimitsClient, region, signer)
            result = []
            for value in pages(c.list_limit_values, tenancy, service.name):
                definition = definitions.get((service.name, value.name))
                result.append({'region': region, 'service': service.name, 'service_description': service.description,
                    'name': value.name, 'limit': safe_limit(value.value), 'scope': value.scope_type,
                    'availability_domain': value.availability_domain or '',
                    'description': definition.description if definition else '',
                    'usage_supported': bool(definition and definition.is_resource_availability_supported)})
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            return [row for group in pool.map(service_limits, services) for row in group]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for region_rows in pool.map(collect_region, REGIONS):
            rows.extend(region_rows)
    local = threading.local()
    def current_use(row):
        if not row['usage_supported']:
            row.update(current_use=None, usage_status='Not supported', utilization=None, alert=False)
            return row
        if not hasattr(local, 'clients'):
            local.clients = {}
        region = row['region']
        if region not in local.clients:
            local.clients[region] = cloud_client(oci.limits.LimitsClient, region, signer)
        kwargs = {'availability_domain': row['availability_domain']} if row['scope'] == 'AD' else {}
        for attempt in range(4):
            try:
                value = local.clients[region].get_resource_availability(row['service'], row['name'], tenancy, **kwargs).data
                used = value.fractional_usage if value.fractional_usage is not None else value.used
                ratio, alert = utilization(row['limit'], used)
                row.update(current_use=used, usage_status='Retrieved' if used is not None else 'Not reported',
                           utilization=ratio, alert=alert)
                return row
            except oci.exceptions.ServiceError as exc:
                if (exc.status == 429 or exc.status >= 500) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                # Do not replace yesterday's complete snapshot with a partial pull.
                raise RuntimeError(f'Usage query failed for {region}/{row["service"]}/{row["name"]}: HTTP {exc.status}') from None
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        rows = list(pool.map(current_use, rows))
    rows.sort(key=lambda r: (r['service'], r['name'], r['region'], r['availability_domain']))
    return {'schema_version': 1, 'pull_started_at': started, 'last_pull_at': now(),
            'schedule': 'Daily at 12:00 UTC', 'regions': list(REGIONS), 'rows': rows,
            'record_count': len(rows), 'usage_count': sum(r['usage_status'] == 'Retrieved' for r in rows)}

def object_store():
    return cloud_client(oci.object_storage.ObjectStorageClient, 'us-ashburn-1')

def snapshot_object(name='snapshot.json'):
    return (os.environ['STORAGE_NAMESPACE'], os.environ['SNAPSHOT_BUCKET'], name)

def refresh():
    store = object_store()
    try:
        snapshot = collect_snapshot()
        body = json.dumps(snapshot, separators=(',', ':'), allow_nan=False).encode()
        store.put_object(*snapshot_object(), body, content_type='application/json')
        store.put_object(*snapshot_object('refresh-status.json'), json.dumps({'status': 'Succeeded', 'last_attempt_at': now()}))
        LOG.info('Snapshot published: %s records', snapshot['record_count'])
        return {'status': 'Succeeded', 'last_pull_at': snapshot['last_pull_at'], 'record_count': snapshot['record_count']}
    except Exception:
        LOG.exception('Snapshot refresh failed; previous snapshot retained')
        store.put_object(*snapshot_object('refresh-status.json'), json.dumps({'status': 'Failed', 'last_attempt_at': now()}))
        raise

def read_snapshot():
    store = object_store()
    data = json.loads(store.get_object(*snapshot_object()).data.content)
    try:
        data['refresh_status'] = json.loads(store.get_object(*snapshot_object('refresh-status.json')).data.content)
    except oci.exceptions.ServiceError as exc:
        if exc.status != 404:
            raise
    return data

def exchange_code(payload):
    code, verifier = payload.get('code', ''), payload.get('verifier', '')
    if not isinstance(code, str) or not 1 <= len(code) <= 2048 or not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
        raise ValueError('Invalid sign-in response')
    values = {'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier,
              'client_id': os.environ['OAUTH_CLIENT_ID'], 'redirect_uri': os.environ['APP_URL']}
    domain = os.environ['IDENTITY_DOMAIN_URL']
    if not domain.startswith('https://') or not urllib.parse.urlparse(domain).hostname.endswith('.identity.oraclecloud.com'):
        raise RuntimeError('Invalid identity-domain configuration')
    request = urllib.request.Request(domain.rstrip('/') + '/oauth2/v1/token',
        data=urllib.parse.urlencode(values).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=20) as result:
            tokens = json.load(result)
        return {key: tokens[key] for key in ('access_token', 'expires_in', 'token_type') if key in tokens}
    except urllib.error.HTTPError:
        raise ValueError('Sign-in expired or was rejected. Please sign in again.') from None

def make_xlsx(rows):
    """Export the authenticated snapshot as a native workbook, without extra services."""
    if not isinstance(rows, list) or len(rows) > 15000:
        raise ValueError('Invalid export row count')
    headers = ['Region', 'Service', 'Limit name', 'Current limit', 'Current use', 'Utilization', 'Scope', 'Availability domain', 'Description']
    def text_cell(address, value, style=0):
        text = str(value or '')
        text = ''.join(c for c in text if ord(c) >= 32 or c in '\t\n\r')[:32000]
        return f'<c r="{address}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    xml_rows = ['<row r="1">' + ''.join(text_cell(chr(65+i)+'1', value, 1) for i,value in enumerate(headers)) + '</row>']
    for index, row in enumerate(rows, 2):
        if not isinstance(row, dict):
            raise ValueError('Invalid export record')
        used = row.get('current_use')
        values = [row.get('region'), row.get('service'), row.get('name'), row.get('limit'),
                  used if used is not None else row.get('usage_status', 'Not reported'), None,
                  row.get('scope'), row.get('availability_domain'), row.get('description')]
        cells = []
        for column, value in enumerate(values):
            address = chr(65+column)+str(index)
            if column == 5:
                formula = f'IF(ISNUMBER(E{index}),IF(VALUE(D{index})&gt;0,E{index}/VALUE(D{index}),IF(E{index}&gt;0,"Over limit","n.a.")),"n.a.")'
                ratio, alert = utilization(row.get('limit', 0), used)
                cached = f'<v>{ratio}</v>' if ratio is not None else '<v>Over limit</v>' if alert else '<v>n.a.</v>'
                kind = '' if ratio is not None else ' t="str"'
                cells.append(f'<c r="{address}" s="2"{kind}><f>{formula}</f>{cached}</c>')
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                cells.append(f'<c r="{address}"><v>{value}</v></c>')
            else:
                cells.append(text_cell(address, value))
        xml_rows.append(f'<row r="{index}">' + ''.join(cells) + '</row>')
    last = len(rows)+1
    sheet = f'''<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="2" width="24" customWidth="1"/><col min="3" max="3" width="60" customWidth="1"/><col min="4" max="6" width="23" customWidth="1"/><col min="7" max="8" width="28" customWidth="1"/><col min="9" max="9" width="90" customWidth="1"/></cols><sheetData>{''.join(xml_rows)}</sheetData><autoFilter ref="A1:I{last}"/><conditionalFormatting sqref="F2:F{max(last,2)}"><cfRule type="expression" dxfId="0" priority="1"><formula>OR(AND(ISNUMBER(F2),F2&gt;=0.8),F2="Over limit")</formula></cfRule></conditionalFormatting></worksheet>'''
    files = {
      '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
      '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml': f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Limits" sheetId="1" r:id="rId1"/></sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>',
      'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/styles.xml': f'<styleSheet xmlns="{NS}"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF283C50"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"/><xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><dxfs count="1"><dxf><font><b/><color rgb="FF991B1B"/></font><fill><patternFill patternType="solid"><fgColor rgb="FFFECACA"/></patternFill></fill></dxf></dxfs></styleSheet>',
      'xl/worksheets/sheet1.xml': sheet,
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return out.getvalue()

def public_config():
    return {'clientId': os.environ.get('OAUTH_CLIENT_ID', ''), 'domain': os.environ.get('IDENTITY_DOMAIN_URL', ''),
            'appUrl': os.environ.get('APP_URL', ''), 'apiUrl': os.environ.get('API_URL', ''),
            'scope': os.environ.get('OAUTH_SCOPE', '')}

def handler(ctx, data: io.BytesIO = None):
    headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'}
    try:
        raw = data.getvalue() if data else b''
        if len(raw) > 5_000_000:
            raise ValueError('Request too large')
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError('Invalid request')
        role = os.environ.get('APP_ROLE', '')
        if role == 'collector':
            result = refresh()
        elif role == 'web':
            if payload.get('action') == 'token':
                result = exchange_code(payload)
            elif not payload:
                html = Path(__file__).with_name('index.html').read_text()
                config = json.dumps(public_config()).replace('<', '\\u003c')
                html = html.replace('__PUBLIC_CONFIG__', config)
                headers['Content-Type'] = 'text/html; charset=utf-8'
                headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                return response.Response(ctx, response_data=html, headers=headers)
            else:
                return response.Response(ctx, response_data='{"error":"Not permitted"}', status_code=403, headers=headers)
        elif role == 'api':
            if payload.get('action') == 'snapshot':
                result = read_snapshot()
            elif payload.get('action') == 'export':
                snapshot = read_snapshot()
                regions = payload.get('regions', [])
                services = payload.get('services', [])
                search = str(payload.get('search', '')).lower()[:200]
                if not isinstance(regions, list) or not isinstance(services, list):
                    raise ValueError('Invalid export filters')
                rows = [r for r in snapshot['rows'] if (not regions or r['region'] in regions)
                    and (not services or r['service'] in services)
                    and (not payload.get('alertsOnly') or r['alert'])
                    and (not search or search in (r['name'] + ' ' + r['description'] + ' ' + r['service']).lower())]
                result = {'xlsx': base64.b64encode(make_xlsx(rows)).decode(),
                    'filename': 'OCI-Limits-' + snapshot['last_pull_at'][:10] + '.xlsx'}
            else:
                raise ValueError('Unknown action')
        else:
            raise RuntimeError('Function role is not configured')
        return response.Response(ctx, response_data=json.dumps(result, separators=(',', ':'), allow_nan=False), headers=headers)
    except (ValueError, json.JSONDecodeError) as exc:
        return response.Response(ctx, response_data=json.dumps({'error': str(exc)}), status_code=400, headers=headers)
    except Exception:
        LOG.exception('Request failed')
        return response.Response(ctx, response_data='{"error":"Data is temporarily unavailable. Please try again shortly."}', status_code=503, headers=headers)
