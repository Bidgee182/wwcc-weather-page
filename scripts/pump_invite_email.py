"""
WWCC Pump Control - Invite Email
=================================
Triggered by workflow_dispatch from admin.html when a new pump control user is added.
Sends a styled invitation email with the one-time invite code.

Environment variables (set in workflow):
  RESEND_API_KEY    - Resend API key (shared mailer, scripts/mailer.py)
  EMAIL_FROM        - From address
  RECIPIENT_EMAIL   - New user's email address
  RECIPIENT_NAME    - New user's name
  INVITE_CODE       - 6-digit one-time invite code
  SITE_URL          - Base URL of the site (optional, defaults to GitHub Pages URL)
"""

import os
import sys
import json
import requests

EMAIL_FROM       = os.environ.get('EMAIL_FROM', 'noreply@wwcc.com.au')
RECIPIENT_EMAIL  = os.environ['RECIPIENT_EMAIL']
RECIPIENT_NAME   = os.environ['RECIPIENT_NAME']
INVITE_CODE      = os.environ['INVITE_CODE']
SITE_URL         = os.environ.get('SITE_URL', 'https://bidgee182.github.io/wwcc-weather-page').rstrip('/')

PUMP_PAGE_URL    = SITE_URL + '/pump-station.html'
LOGO_URL         = SITE_URL + '/assets/images/logo-white.png'

# ─── Email HTML ───────────────────────────────────────────────────────────────

_MOBILE_STYLE = """<style type="text/css">
@media only screen and (max-width:600px) {
  table.outer { width:100% !important; }
  td.mob-logo-cell { display:block !important; width:100% !important; text-align:center !important; padding-right:0 !important; padding-bottom:14px !important; }
  td.mob-logo-cell img { height:40px !important; width:auto !important; max-width:100% !important; }
  td.mob-text-cell { display:block !important; width:100% !important; }
}
</style>"""


def build_html(name, invite_code, pump_url, logo_url):
    first = name.split()[0] if name else name
    code_digits = '  '.join(list(invite_code))  # spaced for readability

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_MOBILE_STYLE}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table class="outer" width="600" cellpadding="0" cellspacing="0"
    style="max-width:600px;width:100%;background:white;border-radius:14px;overflow:hidden;
    box-shadow:0 4px 20px rgba(0,0,0,0.10);">

  <!-- HEADER - black/white -->
  <tr><td bgcolor="#111111" style="background-color:#111111;padding:22px 20px 18px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td class="mob-logo-cell" valign="middle"
            style="padding-right:18px;white-space:nowrap;width:1%;">
          <img src="{logo_url}" width="194" height="44" alt="Wagga Wagga Country Club"
               style="display:block;border:0;">
        </td>
        <td class="mob-text-cell" valign="top">
          <p style="margin:0;font-size:10px;color:#999999;letter-spacing:1.5px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
            WAGGA WAGGA COUNTRY CLUB
          </p>
          <h1 style="margin:7px 0 0 0;font-size:20px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">Pump Control Access</h1>
          <p style="margin:5px 0 0 0;font-size:12px;color:#999999;
              font-family:Arial,sans-serif;">Irrigation Pump Station Dashboard</p>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- BODY -->
  <tr><td style="background:white;padding:32px 28px 28px;">

    <p style="margin:0 0 18px 0;font-size:15px;color:#111111;font-family:Arial,sans-serif;">
      Hi {first},
    </p>
    <p style="margin:0 0 18px 0;font-size:14px;color:#374151;font-family:Arial,sans-serif;line-height:1.6;">
      You have been granted access to the <strong>WWCC Irrigation Pump Station</strong> control dashboard.
      Use the invite code below to activate your account and set your own PIN.
    </p>

    <!-- Invite code block -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:28px 0;">
      <tr><td align="center">
        <table cellpadding="0" cellspacing="0"
            style="border:2px solid #111111;border-radius:12px;padding:0;overflow:hidden;">
          <tr><td bgcolor="#111111" style="background-color:#111111;padding:10px 32px;">
            <p style="margin:0;font-size:10px;color:#999999;letter-spacing:2px;
                text-transform:uppercase;font-family:Arial,sans-serif;text-align:center;">
              ONE-TIME INVITE CODE
            </p>
          </td></tr>
          <tr><td style="padding:20px 40px;text-align:center;background:white;">
            <p style="margin:0;font-size:42px;font-weight:700;letter-spacing:8px;
                color:#111111;font-family:'Courier New',Courier,monospace;">
              {invite_code}
            </p>
          </td></tr>
          <tr><td style="padding:10px 32px;background:#f8f8f8;border-top:1px solid #e5e5e5;text-align:center;">
            <p style="margin:0;font-size:11px;color:#6b7280;font-family:Arial,sans-serif;">
              This code can only be used once. It expires when you set your PIN.
            </p>
          </td></tr>
        </table>
      </td></tr>
    </table>

    <!-- Steps -->
    <div style="margin:24px 0;background:#f9fafb;border-radius:10px;padding:20px 24px;
        border-left:3px solid #111111;">
      <p style="margin:0 0 12px 0;font-size:12px;font-weight:700;color:#111111;
          text-transform:uppercase;letter-spacing:1px;font-family:Arial,sans-serif;">
        How to activate your account
      </p>
      <table cellpadding="0" cellspacing="0" width="100%">
        <tr>
          <td style="padding:5px 0;vertical-align:top;">
            <span style="display:inline-block;width:22px;height:22px;line-height:22px;
                text-align:center;border-radius:50%;background:#111111;color:white;
                font-size:11px;font-weight:700;font-family:Arial,sans-serif;margin-right:10px;">1</span>
          </td>
          <td style="padding:5px 0;font-size:13px;color:#374151;font-family:Arial,sans-serif;vertical-align:middle;">
            Open the <strong>Pump Station dashboard</strong> and click the <strong>Control</strong> tab
          </td>
        </tr>
        <tr>
          <td style="padding:5px 0;vertical-align:top;">
            <span style="display:inline-block;width:22px;height:22px;line-height:22px;
                text-align:center;border-radius:50%;background:#111111;color:white;
                font-size:11px;font-weight:700;font-family:Arial,sans-serif;margin-right:10px;">2</span>
          </td>
          <td style="padding:5px 0;font-size:13px;color:#374151;font-family:Arial,sans-serif;vertical-align:middle;">
            Select your name from the dropdown (<strong>{name}</strong>)
          </td>
        </tr>
        <tr>
          <td style="padding:5px 0;vertical-align:top;">
            <span style="display:inline-block;width:22px;height:22px;line-height:22px;
                text-align:center;border-radius:50%;background:#111111;color:white;
                font-size:11px;font-weight:700;font-family:Arial,sans-serif;margin-right:10px;">3</span>
          </td>
          <td style="padding:5px 0;font-size:13px;color:#374151;font-family:Arial,sans-serif;vertical-align:middle;">
            Enter the invite code above when prompted
          </td>
        </tr>
        <tr>
          <td style="padding:5px 0;vertical-align:top;">
            <span style="display:inline-block;width:22px;height:22px;line-height:22px;
                text-align:center;border-radius:50%;background:#111111;color:white;
                font-size:11px;font-weight:700;font-family:Arial,sans-serif;margin-right:10px;">4</span>
          </td>
          <td style="padding:5px 0;font-size:13px;color:#374151;font-family:Arial,sans-serif;vertical-align:middle;">
            Set your own private PIN and enter the master password (ask the administrator)
          </td>
        </tr>
      </table>
    </div>

    <!-- CTA button -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0 8px;">
      <tr><td align="center">
        <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{pump_url}" style="height:44px;v-text-anchor:middle;width:260px;" arcsize="50%" stroke="f" fillcolor="#111111"><w:anchorlock/><center style="color:white;font-family:Arial,sans-serif;font-size:13px;font-weight:bold;">Open Pump Station Dashboard</center></v:roundrect><![endif]-->
        <!--[if !mso]><!-->
        <a href="{pump_url}"
            style="display:inline-block;background:#111111;color:white;text-decoration:none;
            font-size:13px;font-weight:700;padding:13px 32px;border-radius:24px;
            font-family:Arial,sans-serif;letter-spacing:0.5px;">
          Open Pump Station Dashboard
        </a>
        <!--<![endif]-->
      </td></tr>
    </table>

    <p style="margin:20px 0 0 0;font-size:11px;color:#9ca3af;font-family:Arial,sans-serif;line-height:1.5;">
      If the button does not open, copy this link into your browser:<br>
      <a href="{pump_url}" style="color:#374151;">{pump_url}</a>
    </p>

  </td></tr>

  <!-- FOOTER -->
  <tr><td bgcolor="#111111" style="background-color:#111111;padding:20px 28px;text-align:center;">
    <div style="font-size:10px;color:#666666;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:6px;font-family:Arial,sans-serif;">
      Wagga Wagga Country Club - Automated Notification
    </div>
    <div style="font-size:11px;color:#555555;font-family:Arial,sans-serif;">
      Do not reply to this email. Contact the site administrator if you have questions.
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ─── Send ─────────────────────────────────────────────────────────────────────

def send(name, email, invite_code, pump_url, logo_url):
    html = build_html(name, invite_code, pump_url, logo_url)
    subject = f'WWCC Pump Control - Your access invite code'
    from mailer import send_html
    ok, detail = send_html(subject, html, [f'{name} <{email}>'], stream='pump')
    if not ok:
        print(f'Resend error: {detail}', file=sys.stderr)
        sys.exit(1)
    print(f'Invite email sent to {email} ({name})')


if __name__ == '__main__':
    send(RECIPIENT_NAME, RECIPIENT_EMAIL, INVITE_CODE, PUMP_PAGE_URL, LOGO_URL)
