INVITE sip:srs@srs.linda.example.com:5061;transport=tls SIP/2.0
Via: SIP/2.0/TLS sbce-edge-1.avaya.example.com:5061;branch=z9hG4bK-avaya-sbce-aa11
From: <sip:recorder@avaya-sbce.example.com>;tag=sbce-tag-aa11
To: <sip:srs@srs.linda.example.com:5061>
Call-ID: avaya-rec-fixture-0001@sbce-edge-1.avaya.example.com
CSeq: 1 INVITE
Max-Forwards: 70
Contact: <sip:recorder@sbce-edge-1.avaya.example.com:5061;transport=tls>
Require: siprec
User-Agent: Avaya-SBCE/10.1.2
Content-Type: multipart/mixed; boundary="avaya-bound-bb22"
Content-Length: 1714

--avaya-bound-bb22
Content-Type: application/sdp

v=0
o=AvayaSBCE 9876 5432 IN IP4 198.51.100.7
s=SIPREC
c=IN IP4 198.51.100.7
t=0 0
m=audio 30000 UDP/TLS/RTP/SAVP 0
c=IN IP4 198.51.100.7
a=rtpmap:0 PCMU/8000
a=ptime:20
a=label:agent
a=sendonly
a=fingerprint:sha-256 0A:1B:2C:3D:4E:5F:60:71:82:93:A4:B5:C6:D7:E8:F9:10:21:32:43:54:65:76:87:98:A9:BA:CB:DC:ED:FE:00
a=setup:actpass
m=audio 30002 UDP/TLS/RTP/SAVP 0
c=IN IP4 198.51.100.7
a=rtpmap:0 PCMU/8000
a=ptime:20
a=label:customer
a=sendonly
a=fingerprint:sha-256 0A:1B:2C:3D:4E:5F:60:71:82:93:A4:B5:C6:D7:E8:F9:10:21:32:43:54:65:76:87:98:A9:BA:CB:DC:ED:FE:01
a=setup:actpass
--avaya-bound-bb22
Content-Type: application/rs-metadata+xml

<?xml version="1.0" encoding="UTF-8"?>
<recording xmlns="urn:ietf:params:xml:ns:recording:1">
  <session session_id="avaya-rec-session-fixture-0001" state="active"/>
  <participant participant_id="part-agent-001">
    <nameID aor="sip:agent42@avaya.example.com">
      <name>Michael Chen</name>
    </nameID>
    <associate-time>2026-05-07T14:35:10Z</associate-time>
  </participant>
  <participant participant_id="part-customer-001">
    <nameID aor="tel:+15557654321"/>
    <associate-time>2026-05-07T14:35:10Z</associate-time>
  </participant>
  <stream stream_id="stream-agent-001" session_id="avaya-rec-session-fixture-0001">
    <label>agent</label>
  </stream>
  <stream stream_id="stream-customer-001" session_id="avaya-rec-session-fixture-0001">
    <label>customer</label>
  </stream>
  <participantstreamassoc participant_id="part-agent-001">
    <send>stream-agent-001</send>
  </participantstreamassoc>
  <participantstreamassoc participant_id="part-customer-001">
    <send>stream-customer-001</send>
  </participantstreamassoc>
</recording>
--avaya-bound-bb22--
