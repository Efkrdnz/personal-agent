# Getting a +90 number that Jarvis can actually call from

**Status:** researched 16 Sep 2026. One phone call decides it. Everything below is desk research — every
Turkish operator and regulator domain (`netgsm.com.tr`, `bilgibankasi.netgsm.com.tr`, `verimor.com.tr`,
`btk.gov.tr`, `mevzuat.gov.tr`) is unreachable from any research sandbox, so operator-side facts are drawn
from search-engine summaries of those operators' own indexed pages, not read at source. Confidence is
flagged throughout. **No further desk research can settle this — the phone call can.**

## Bottom line

An individual in Turkey can almost certainly get a programmable +90 number. The earlier "closed to
individuals" conclusion was wrong: it generalised from Verimor, which happens to be the one operator that
genuinely demands a `vergi levhası`.

The real question is not eligibility but **capability**, and it has one decisive sub-question:

> Can you originate *outbound* calls from your own PBX, presenting your own +90 number?

Netgsm's documented "SIP Trunk" feature is an **inbound redirect** — it forwards incoming calls to an IP
you nominate. Outbound is documented as coming from a **registered extension** against
`sip.netgsm.com.tr`. So outbound with your CLI looks available, but by registration rather than by trunk,
and that must be confirmed before you commit.

### The structural finding that matters most

**Outbound +90 caller ID only works if the call originates domestically.** Every international-DID route is
dead for the restaurant use case, for three independent reasons that each bite on their own:

- Zadarma and the cheap international +90 DID class are sold **inbound-only** — their own Turkish city pages
  say the number "is not intended for outgoing calls to Turkey".
- Telnyx and AVOXI TrueLocal *can* present a +90 CLI legitimately, but only to verified Turkish **companies**
  (İmza Sirküleri, Vergi Levhası, Faaliyet Belgesi, Ticaret Sicil Gazetesi).
- Turkey runs **BTKCLI**, a BTK-backed real-time system operated with Turkcell / Türk Telekom / Vodafone that
  intercepts calls arriving from foreign networks presenting a Turkish CLI, checks whether that line is
  genuinely roaming, and terminates the call if it is not. Turkcell reported blocking ~4 million such calls
  in December 2025.

  *Caveat, stated honestly:* the GSMA evidence describes screening of Turkish **mobile** CLIs on foreign
  interconnect. It is **not** confirmed that geographic 212/216/312 CLIs are screened the same way, and
  BTKCLI does not touch domestically-originated calls at all. So BTKCLI is not an independent second killer —
  it kills one specific pattern. The provider-side rules bite first, and the net answer is unchanged.

Twilio's "Verified Caller ID" trick — verify your own Turkish mobile, then present it from abroad — is
*exactly* the foreign-origination-with-non-roaming-Turkish-CLI pattern BTKCLI exists to catch. Don't.

**Conclusion: originate domestically, or use your own SIM. Do not try to present a +90 CLI from a datacentre
abroad.**

---

## Do this first (30 minutes)

Phone Netgsm on **0850 303 0 303** (alt **0312 911 0 911**). Ask questions 1 and 3 below first and do not let
them move on until you have a clear answer to 3. Write the answers down verbatim, especially on DTMF.

- **Both yes** → start the e-Devlet application the same hour. Have your chip ID card and an NFC phone ready.
- **Either no** → stop, and spend the evening on the backup instead. It costs nothing and gives you a genuine
  +90 caller ID tonight.

### The eight questions, in Turkish

1. **Şahıs şirketim veya vergi levham yok. Sadece T.C. Kimlik numaramla, bireysel abone olarak sabit telefon
   hizmeti alabiliyor muyum?**
   *No sole proprietorship, no tax certificate — can I subscribe as an individual with only my T.C. Kimlik No?*

2. **Bireysel abonelikte bulunduğum ilin coğrafi numarasını (0212 / 0216 / 0312) alabiliyor muyum, yoksa
   bireysel hesaplara yalnızca 0850'li numara mı tanımlanıyor?**
   *Can an individual account hold a geographic number for my province, or only 0850?*

3. **EN ÖNEMLİ SORU — Kendi sunucumdaki Asterisk santralimi SIP kullanıcı adı ve şifresiyle
   `sip.netgsm.com.tr` adresine register ederek DIŞARIYA arama başlatabilir miyim? Bu aramalarda karşı tarafa
   benim tahsisli numaram mı görünür?**
   *THE decisive question — can I register my own Asterisk PBX and originate outbound calls, and will the
   called party see my allocated number?*

4. **SIP Trunk özelliği yalnızca GELEN çağrıları benim sunucuma yönlendiriyor, doğru mu? Giden aramalar için
   ayrıca register olan bir dahili mi gerekiyor?**
   *Is the SIP Trunk feature inbound-only, with outbound needing a registered extension?*

5. **Trunk üzerinde DTMF nasıl iletiliyor: RFC 2833 / RFC 4733 telephone-event mi, SIP INFO mu, yoksa sadece
   ses içinde (in-band) mi? IVR'sız, kendi sunucumda DTMF algılamam gerekiyor.**
   *How is DTMF carried? I need to detect it on my own server, without your IVR.*

6. **Trunk hangi ses kodeklerini kullanıyor? G.711 A-law (alaw) sabitleyebilir miyim?**
   *Which codecs? Can I pin to G.711 A-law?*

7. **Gelen çağrılar hangi IP adresinden/IP bloğundan geliyor? Ayrıca kaç eşzamanlı kanal destekleniyor?**
   *Which source IP should I whitelist, and how many concurrent channels?*

8. **Hattı kişisel bir yapay zeka asistanı için kullanacağım: kendi cep telefonumla konuşmak ve kendi adıma
   restoran rezervasyonu yaptırmak için. Ticari pazarlama araması yapmayacağım. Bu kullanım için giden arama
   izni gerekiyor mu?**
   *Personal AI assistant, no marketing calls — is outbound permission needed?*

### Kill criteria — switch to the backup immediately if you hear any of these

1. "Bireysel abonelikte SIP/santral hizmeti verilmiyor" or "vergi levhası zorunlu" — SIP is gated to Kurumsal.
2. Outbound cannot be originated from your own registered endpoint, **or** outbound presents a Netgsm service
   number rather than your allocated number.
3. DTMF is in-band only with no telephone-event or SIP INFO option. This breaks the PIN gate and there is no
   software fix on a compressed voice path.

---

## Primary path — Netgsm bireysel subscription

**Why Netgsm:** it is the only Turkish operator with (i) a publicly reopened individual subscription type
that does not require a `vergi levhası`, (ii) identity-based e-Devlet onboarding rather than a business check,
(iii) a self-service SIP configuration page exposing both a trunk redirect target and SIP credentials, and
(iv) first-party AI-calling integration guides (VAPI, Retell, ElevenLabs) — proving they actively support
machine-driven calling on that trunk.

Evidence that a `bireysel` type exists (all search-index-derived, *medium* confidence): Netgsm's fixed-line
contract is concluded with "the gerçek veya tüzel kişi who applies"; the same text caps a natural person at
15 *bireysel* subscriptions against 50 *kurumsal* per tax number; and the number-porting guide warns against
"bireysel başvuru için kurumsal seçim yapılması", instructing "Şahıs işletmesiyseniz Kurumsal seçeneğini
seçmeniz gerekir" — so a sole proprietorship is routed to Kurumsal, implying Bireysel is for a plain natural
person.

**Verimor, for contrast, is genuinely closed:** "Bireysel abonelere hizmet verilmemektedir" and "abonelik
başvurusu için vergi levhası zorunludur". But it accepts `şahıs şirketleri` — so even there, the gate is a
registered sole proprietorship, not a real company.

### Steps

1. Make the phone call above.
2. If positive: `netgsm.com.tr` → Abonelik → Yeni Abonelik → **BİREYSEL** (*not* Kurumsal — that's for şahıs
   işletmesi). Ask explicitly for a `coğrafi numara` in your own province; take 0850 only if refused.
3. Approve the contract via e-Devlet: log in with T.C. Kimlik No → *e-Kayıt Başvurusu Onay İşlemleri (BTK)* →
   Haberleşme → Netgsm → approve, then NFC-verify by scanning the QR with the e-Devlet mobile app and reading
   your chipped TCKK.
4. While activation is pending, rent a small VPS **in Istanbul** (~€4–6/mo) with a static IP and install
   Asterisk (`res_pjsip`). Istanbul over Frankfurt: lower latency both to Netgsm and to the far end, and a
   Turkish peer IP if Netgsm ever objects to a foreign one.
5. Once active: *Ses Hizmeti → Ayarlar → SIP Bilgileri*. Record SIP server, username, password. Enable the
   SIP Trunk feature, target your VPS IP.
6. Configure Asterisk **both ways on the same box** — see below.
7. Bridge media to Gemini Live via AudioSocket (8 kHz slin over TCP, resample to 16 kHz) or ARI
   `externalMedia`.
8. Test in order: (i) call your number, confirm DTMF arrives as events in `asterisk -rvvv`; (ii) have Jarvis
   call your mobile, confirm the CLI is *your* number not a Netgsm service number; (iii) call one real
   restaurant.

### Documents

T.C. Kimlik No · chipped TCKK or Mavi Kart (physically present, for the NFC read) · NFC smartphone with the
e-Devlet app · e-Devlet şifresi · Turkish mobile + email for OTP · address matching your `ikametgah` (a
geographic number must match your province) · Turkish card for payment.

**If you are asked for a `vergi levhası` on the bireysel path, stop — you selected the wrong application type.**

### Cost and lead time

No setup fee, no monthly fee on the number. **249 TL/year** number usage and support fee (reproduced
independently across searches). Outbound minutes bought as a `ses hizmeti` package — Netgsm's own tariff could
not be retrieved; Verimor's published 0,990 TL/dk is the right order of magnitude, so a few hundred minutes is
a few hundred TL. Plus VPS €4–6/mo. **Realistic all-in: 250–1.000 TL/month.**

Do **not** budget for Netsipp/Netsantral (their cloud PBX, ~403 TL/mo) — you are running your own.

Call day 1 → apply day 1 → active day 2–3 → first real restaurant call day 3–5.

---

## Backup — your own SIM in a GSM-to-SIP bridge

No operator application, no BTK anything, no company, no approval queue. You already own the SIM; the only
wait is shipping. **And it is technically *better* than the PSTN path in two ways.**

**The decisive advantage:** the gateway does not assert the caller ID — the mobile network does, from the
SIM's own MSISDN. Outbound calls present your real +90 5xx number with full A-number CLI, indistinguishable
from you dialling by hand. Nothing is spoofed, so nothing can be screened.

**Variant 1 — zero hardware, testable tonight.** Asterisk `chan_mobile`, the Bluetooth Handsfree-Profile
channel driver in Asterisk master. Pair your existing phone to the Asterisk box over Bluetooth; Asterisk
places and answers calls on your real SIM. Dialable as `Dial(Mobile/device/NNN)`. **HFP wideband speech
(mSBC) is 16 kHz — a direct match for Gemini Live's native input rate, better than any PSTN path**, which is
8 kHz and needs resampling.

**Variant 2 — $25–60.** A Quectel EC25-E or SimCOM SIM7600G-H USB module on a Pi or mini-PC with
`asterisk-chan-quectel`. Module-side DTMF detection via `AT+QTONEDET`/`AT+DDET` — materially more reliable
than DSP on post-AMR audio — and a `slin16` option giving 16 kHz on SIM7600X.

Appliance gateways are a worse buy; the Yeastar TG100 quoted in earlier research is end-of-life.

**Unverified:** nobody has tested `chan_mobile` inbound DTMF or audio quality on a Turkcell / Vodafone /
Türk Telekom SIM. Inbound DTMF over HFP is the weak point — test it before relying on the PIN gate.

---

## Fallback if both fail — split the problem

Stop paying for a +90 number you cannot get.

Use cases (a) *you call Jarvis* and (b) *Jarvis calls you* are **fully solved today** by a Twilio or Telnyx
US/UK DID at ~$1–5/mo plus $0.02–0.15/min to Turkey. These are exactly the IP-or-digest-on-INVITE,
telephone-event, A-law elastic trunks the stack expects; DTMF arrives as RFC 2833 so the PIN gate is reliable;
and it does not matter that the number is foreign because *you know to expect the call*.

Only use case (c), the restaurants, actually needs +90 — and there the failure is **social, not technical**: a
Turkish restaurant seeing a +1/+44 number at 19:00 on a Friday frequently will not answer, and when it does,
staff often hang up inside two seconds — precisely the window a spoken Turkish opener never gets.

If you must ship it anyway: zero ringback delay, and an unmistakably human first line inside 1.5 seconds
("Merhaba, [isim] Bey adına rezervasyon için arıyorum"), with the AI disclosure after the first exchange
rather than at the top.

---

## Technical integration

### LiveKit Cloud cannot be the SIP endpoint on its own — verified in code

This corrects the earlier architecture recommendation. `livekit_sip.proto`'s `SIPInboundTrunkInfo` and
`SIPOutboundTrunkInfo` contain **no registrar, contact, expiry or binding field of any kind**. Outbound
carries only `{address, transport, numbers, auth_username, auth_password, from_host, ...}` — a digest response
to a 401/407 on an INVITE, not a registration. Issue `livekit/sip#338` shows a REGISTER to a LiveKit Cloud
inbound trunk answered **405 Method Not Allowed**. PR `livekit/sip#774` ("feat: outbound SIP registration")
was still **open** as of 16 Sep 2026, 3 commits, awaiting a first approving review, with a contributor noting
the day before that it had "been quiet for a few weeks". Self-hosting `livekit/sip` does not help — same code.

Netgsm outbound appears to need REGISTER. **Therefore: Asterisk terminates SIP, not LiveKit.**

If you still want LiveKit's agent framework, run `livekit/sip` on localhost *behind* Asterisk, or put
Kamailio's `uac` module in front purely as a REGISTER relay — but if you are running that box anyway,
terminate media on Asterisk and skip the extra element.

### Asterisk configuration — both directions on one box

Netgsm appears to use **different auth in each direction**:

- **Inbound: IP/host-based.** Their SIP Trunk page has you enter your server's IP or hostname plus port and a
  prefix. Their own ElevenLabs guide enters an arbitrary external host (`sip.rtc.elevenlabs.io`, transport
  TCP), and an indexed example even shows a `*.sip.livekit.cloud` host — proof they will INVITE an arbitrary
  foreign host.
- **Outbound: registration-based** against `sip.netgsm.com.tr` with the portal credentials.

So configure both: a `type=registration` + `type=auth` + `type=aor` set pointing at `sip.netgsm.com.tr`
(`retry_interval=60`, `expiration=3600`) for outbound, and an `identify`/`endpoint` matching Netgsm's gateway
IP for inbound.

```
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
external_media_address=<VPS public IP>
direct_media=no          ; you must own the media to grab PCM for Gemini
disallow=all
allow=alaw,ulaw
```

Get the exact inbound source IP from support and whitelist it. **Do not run `0.0.0.0/0`.**

---

## What remains unverified

Listed plainly, because several of these could change the plan:

- **Everything operator-side is search-index-derived, not read at source.** `curl` returns HTTP 000 for
  `bilgibankasi.netgsm.com.tr`, `www.verimor.com.tr` and `www.btk.gov.tr`. Three research passes and three
  adversarial reviews all hit the same wall.
- Whether a Netgsm **bireysel** account can hold a **geographic** number or only 0850 — sources conflict.
- Whether Netgsm permits outbound origination from a **customer's own registering PBX** (as opposed to their
  softphone app), and whether the allocated number is presented as CLI. *This is question 3, and the project
  turns on it.*
- **Netgsm's DTMF mode.** Documented in writing by exactly one Turkish provider — Verimor's
  `dtmfmode=info&rfc2833` — and extrapolated to Netgsm by analogy. That is an inference from a different
  company about a hard requirement.
- Netgsm's outbound tariff, inbound gateway IP/CIDR, transports, **concurrent channel limit** (one wholesale
  vendor documents a 2-channel cap per Turkish DID), and codec list.
- Whether any Turkish operator requires separate approval for **automated/AI outbound calling**. One
  Verimor/ElevenLabs guide mentions outbound campaigns starting only "once your outbound calling permission is
  approved". No operator was found to *prohibit* AI calling.
- Whether BTKCLI screens **geographic** CLIs on foreign interconnect or only mobile ones.
- The entire Turkish regulatory picture — `btk.gov.tr` and `mevzuat.gov.tr` are blocked. The claim that no
  regulatory bar exists on an individual holding a number is inferred, not read.
- Reported late-2025/2026 BTK decisions (2025/DK-YED/399, 2025/DK-YED/412, in force 1 April 2026) and a
  reported TBMM package on SIM-line caps. Secondary Turkish tech-news reporting only; enacted texts unread.
- `chan_mobile`'s real-world behaviour on a Turkish SIM — the HFP wideband capability is confirmed in the
  Asterisk source tree, but inbound DTMF over HFP is untested.
