INVITE sip:srs@srs.linda.example.com:5061;transport=tls SIP/2.0
Via: SIP/2.0/TLS perimeta.metaswitch.example.com:5061;branch=z9hG4bK-meta-3344
From: <sip:rec@metaswitch.example.com>;tag=meta-44ff
To: <sip:srs@srs.linda.example.com:5061>
Call-ID: metaswitch-rec-fixture-0001@perimeta.metaswitch.example.com
CSeq: 1 INVITE
Max-Forwards: 70
Contact: <sip:rec@perimeta.metaswitch.example.com:5061;transport=tls>
Require: siprec
User-Agent: Metaswitch-Perimeta/4.6
Content-Type: multipart/mixed; boundary=meta-boundary-cc33
Content-Length: 1612

--meta-boundary-cc33
Content-Type: application/sdp

v=0
o=PerimetaSBC 5500 5500 IN IP4 203.0.113.45
s=Recorded
c=IN IP4 203.0.113.45
t=0 0
m=audio 19000 RTP/SAVP 0
c=IN IP4 203.0.113.45
a=rtpmap:0 PCMU/8000
a=ptime:20
a=label:caller
a=sendonly
a=crypto:1 AES_256_CM_HMAC_SHA1_80 inline:bWV0YS1mYWtlLWtleS1jYWxsZXItc3RyZWFtLTEyMzQ1Njc4OTAxMjM0NTY=
m=audio 19002 RTP/SAVP 0
c=IN IP4 203.0.113.45
a=rtpmap:0 PCMU/8000
a=ptime:20
a=label:callee
a=sendonly
a=crypto:1 AES_256_CM_HMAC_SHA1_80 inline:bWV0YS1mYWtlLWtleS1jYWxsZWUtc3RyZWFtLTEyMzQ1Njc4OTAxMjM0NTY=
--meta-boundary-cc33
Content-Type: application/rs-metadata+xml

<?xml version="1.0" encoding="UTF-8"?>
<recording xmlns="urn:ietf:params:xml:ns:recording:1">
  <session session_id="meta-rec-session-fixture-0001" state="active"/>
  <participant participant_id="caller-1">
    <nameID aor="sip:+13105550100@metaswitch.example.com"/>
    <associate-time>2026-05-07T14:40:00Z</associate-time>
  </participant>
  <participant participant_id="callee-1">
    <nameID aor="sip:agent99@metaswitch.example.com">
      <name>Jamie Doe</name>
    </nameID>
    <associate-time>2026-05-07T14:40:00Z</associate-time>
  </participant>
  <stream stream_id="stream-caller" session_id="meta-rec-session-fixture-0001">
    <label>caller</label>
  </stream>
  <stream stream_id="stream-callee" session_id="meta-rec-session-fixture-0001">
    <label>callee</label>
  </stream>
  <participantstreamassoc participant_id="caller-1">
    <send>stream-caller</send>
  </participantstreamassoc>
  <participantstreamassoc participant_id="callee-1">
    <send>stream-callee</send>
  </participantstreamassoc>
</recording>
--meta-boundary-cc33--
