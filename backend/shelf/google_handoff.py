"""Supported connected-tool command mapping; this module never calls a service.

The authenticated app reserves one attempt. A supervised operator may execute it
only after the human's exact account/action authorization, then read back the
provider record. No caller-supplied idempotency key is supported by these tools;
uncertain creates are searched/read, never automatically repeated.
"""
from datetime import timedelta
from email.message import EmailMessage
from email.policy import SMTP
from html import escape
import re

from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest, instant


def command(calendar, kind, key):
    if calendar.event.status!='tentative' or kind not in ('calendar','email'):
        raise Blocked('An explicitly unbooked owner planning hold is required.')
    marker='Cabinet handoff '+key
    description=calendar.description+'\n'+marker
    if kind=='calendar':
        args={'calendar_id':'primary','title':calendar.event.title,'description':description,
            'start_time':calendar.event.start,'end_time':calendar.event.end,'timezone_str':calendar.event.timezone,
            'location':calendar.event.location,'attendees':[],'self_attendance':'omit','visibility':'private',
            'transparency':'opaque','guests_can_modify':False,'add_google_meet':False}
        tool='google_calendar.create_event'
    else:
        args={'to':'me','subject':calendar.event.title+' ['+key[:12]+']',
            'payload':{'mime_type':'text/html','charset':'utf-8','body':{'content':'<pre>'+escape(description)+'</pre>'}}}
        tool='gmail.send_email'
    return {'tool':tool,'arguments':args,'kind':kind,'key':key,'marker':marker,'uid':calendar.uid,
        'revision':calendar.revision,'payload_digest':digest([tool,args]),
        'requires_human_account_and_action_authorization':True,
        'execution':'Supervised connected tools only; no backend OAuth or automatic execution.',
        'unknown_rule':'Never repeat this write after an uncertain result. Use the read-only reconciliation instructions.'}


def reconciliation(packet):
    args=packet['arguments']
    if packet['kind']=='calendar':
        start=instant(args['start_time'])-timedelta(days=1);end=instant(args['end_time'])+timedelta(days=1)
        return {'tool':'google_calendar.search_events','arguments':{'calendar_id':'primary','query':packet['marker'],
            'time_min':start.isoformat(),'time_max':end.isoformat(),'timezone_str':args['timezone_str'],'max_results':10},
            'follow_up':'Read each returned event by its exact ID. Zero results or multiple matches stay unknown; do not create again.'}
    return {'tool':'gmail.search_email_ids','arguments':{'query':'in:sent "'+packet['marker']+'"','max_results':10},
        'follow_up':'Read full messages by exact IDs. Confirm the single sent record and current connected account. Zero or multiple matches stay unknown; never resend.'}


def verify_readback(packet, evidence):
    """Validate normalized actual read-back, supplied only by the trusted operator.

    A JSON object is not self-authenticating. The operator obtains these fields
    from the connected read tools; there is deliberately no browser upload path.
    """
    if (type(evidence) is not dict or set(evidence)!={'account_email','records'}
            or not isinstance(evidence['account_email'],str)
            or not re.fullmatch(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}',evidence['account_email'])
            or type(evidence['records']) is not list or len(evidence['records'])!=1):
        raise Blocked('One unambiguous provider read-back and resolved account required.')
    record=evidence['records'][0];args=packet['arguments']
    if not isinstance(record,dict) or not isinstance(record.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',record['id']):
        raise Blocked('Observed provider identity required.')
    if packet['kind']=='calendar':
        expected={'calendar_id','id','summary','description','start','end','timezone','location','visibility','attendees','status'}
        if (set(record)!=expected or record['calendar_id']!='primary' or record['summary']!=args['title']
                or record['description']!=args['description'] or instant(record['start'])!=instant(args['start_time'])
                or instant(record['end'])!=instant(args['end_time']) or record['timezone']!=args['timezone_str']
                or record['location']!=args['location'] or record['visibility']!='private'
                or record['attendees']!=[] or record['status']!='confirmed'):
            raise Blocked('Calendar read-back does not match the approved owner hold.')
    else:
        expected={'id','label_ids','to','subject','html','cc','bcc'}
        if (set(record)!=expected or type(record['label_ids']) is not list or 'SENT' not in record['label_ids']
                or record['to']!=[evidence['account_email']] or record['cc']!=[] or record['bcc']!=[]
                or record['subject']!=args['subject'] or record['html']!=args['payload']['body']['content']):
            raise Blocked('Sent-message read-back does not match the approved owner packet.')
    return {'provider_id':record['id'],'account_digest':digest(evidence['account_email'].casefold()),
        'evidence_digest':digest(evidence),'payload_digest':packet['payload_digest'],
        'state':'recorded_calendar' if packet['kind']=='calendar' else 'recorded_sent'}


def email_file(calendar):
    message=EmailMessage(policy=SMTP)
    message['Subject']=calendar.event.title
    message['X-Unsent']='1'
    # Deliberately no To/From guessed from a person's name or browser session.
    message.set_content(calendar.description+'\n\nUnsent owner packet. Choose your own address before sending. No tickets attached.')
    return message.as_bytes()
