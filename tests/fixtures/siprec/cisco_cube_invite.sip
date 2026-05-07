INVITE sip:srs@srs.linda.example.com:5061;transport=tls SIP/2.0
Via: SIP/2.0/TLS sbc.cisco-cube.example.com:5061;branch=z9hG4bK-cisco-cube-001
From: <sip:cube-recorder@cisco-cube.example.com>;tag=cube-call-001
To: <sip:srs@srs.linda.example.com:5061>
Call-ID: cube-rec-fixture-0001@cisco-cube.example.com
CSeq: 1 INVITE
Max-Forwards: 70
Contact: <sip:cube-recorder@cube.example.com:5061;transport=tls>
Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, SUBSCRIBE, NOTIFY, UPDATE, PRACK
Supported: replaces, 100rel, timer, gruu, path
Require: siprec
User-Agent: Cisco-SIPGateway/IOS-17.13.1a
Content-Type: multipart/mixed;boundary=cube-boundary-7e1f23
Content-Length: 1683

--cube-boundary-7e1f23
Content-Type: application/sdp

v=0
o=CiscoSystemsSIP-GW-UserAgent 1234 5678 IN IP4 192.0.2.10
s=SIP Call
c=IN IP4 192.0.2.10
t=0 0
m=audio 24000 RTP/SAVP 0 8
c=IN IP4 192.0.2.10
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=ptime:20
a=label:1
a=sendonly
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZGUgZmFrZSBzcnRwIGtleSAwMDAxMjM0NQ==
m=audio 24002 RTP/SAVP 0 8
c=IN IP4 192.0.2.10
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=ptime:20
a=label:2
a=sendonly
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:c2RlcyBmYWtlIHNydHAga2V5IHN0cmVhbS0wMDAyAA==
--cube-boundary-7e1f23
Content-Type: application/rs-metadata+xml
Content-Disposition: recording-session

<?xml version="1.0" encoding="UTF-8"?>
<recording xmlns="urn:ietf:params:xml:ns:recording:1">
  <session session_id="cube-rec-session-fixture-0001" state="active"/>
  <participant participant_id="participant-agent">
    <nameID aor="sip:agent-12@cisco-cube.example.com">
      <name>Sarah Anderson</name>
    </nameID>
    <associate-time>2026-05-07T14:32:01Z</associate-time>
  </participant>
  <participant participant_id="participant-customer">
    <nameID aor="sip:+15551234567@pstn.gateway"/>
    <associate-time>2026-05-07T14:32:01Z</associate-time>
  </participant>
  <stream stream_id="stream-1" session_id="cube-rec-session-fixture-0001">
    <label>1</label>
  </stream>
  <stream stream_id="stream-2" session_id="cube-rec-session-fixture-0001">
    <label>2</label>
  </stream>
  <participantstreamassoc participant_id="participant-agent">
    <send>stream-1</send>
  </participantstreamassoc>
  <participantstreamassoc participant_id="participant-customer">
    <send>stream-2</send>
  </participantstreamassoc>
  <communicationsession session_id="cube-rec-session-fixture-0001">
    <sipSessionID>cube-rec-fixture-0001@cisco-cube.example.com</sipSessionID>
  </communicationsession>
</recording>
--cube-boundary-7e1f23--
