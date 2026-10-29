"""Extract the fixed Europe PMC open-access JATS route, with bounded XML."""
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET


def paper_id(url):
    endpoint = urlsplit(url or '')
    if (endpoint.scheme != 'https' or endpoint.hostname != 'www.ebi.ac.uk'
            or endpoint.port not in (None, 443) or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment):
        return None
    match = re.fullmatch(r'/europepmc/webservices/rest/(PMC[0-9]+)/fullTextXML', endpoint.path)
    return match[1] if match else None


def extract(body, pmcid, *, limit=24000):
    empty = dict(status='unavailable', text='', error_code='unreadable_xml')
    # ElementTree does not fetch external DTDs. Reject custom entities outright
    # (including internal expansion), and require UTF-8 before parsing.
    try:
        text = body.decode('utf-8-sig')
        if '<!ENTITY' in text.upper() or len(body) > 2 * 1024 * 1024:
            return empty
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError, ValueError):
        return empty
    if root.tag != 'article':
        return empty
    ids = {e.text for e in root.findall('./front/article-meta/article-id')
           if e.get('pub-id-type') in ('pmc', 'pmcid')}
    if not {pmcid, pmcid[3:]} & ids:
        return dict(empty, error_code='paper_identity_mismatch')
    license_node = root.find('./front/article-meta/permissions/license')
    article_body = root.find('./body')
    if license_node is None or article_body is None:
        return dict(empty, error_code='open_paper_unavailable')
    # Include the provider's license statement and paragraphs, excluding refs,
    # scripts and executable links. Selected text is always marked partial.
    parts = [' '.join(''.join(license_node.itertext()).split())]
    for node in article_body.iter():
        if node.tag in ('p', 'title'):
            parts.append(' '.join(''.join(node.itertext()).split()))
    output = '\n\n'.join(p for p in parts if p)[:limit]
    if len(output) < 100:
        return dict(empty, error_code='insufficient_article_text')
    return dict(status='partial_body', text=output, error_code=None)
