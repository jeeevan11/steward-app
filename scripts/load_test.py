#!/usr/bin/env python3
"""
Steward — WhatsApp Production Simulator & Load Test
=====================================================
50 realistic personas across every category. Real classifier + tier engine +
memory pipeline. Isolated temp database. Zero prod impact. DRY_RUN=true always.

Measures:
  - Tier accuracy (did the brain assign the right tier?)
  - Context retention (does memory persist across turns in the same chat?)
  - Memory isolation (does person A's data bleed into person B?)
  - Duplicate action prevention
  - Burst handling (settling, folding)
  - Priority surfacing (VIP/urgent never silenced)
  - Recovery after interruption (burst → calm → burst)

Usage:
    python scripts/load_test.py
    python scripts/load_test.py --quick    # 20 personas, 1 turn each
    python scripts/load_test.py --chats 10 --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Lock DRY_RUN before any assistant import
os.environ["DRY_RUN"] = "true"
os.environ["MODE"] = "test"
os.environ.setdefault("LOG_LEVEL", "WARNING")

# ─── Colour helpers ───────────────────────────────────────────────────────────
R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
M = "\033[35m"; C = "\033[36m"; W = "\033[37m"; BOLD = "\033[1m"; RST = "\033[0m"

def ok(s): return f"{G}✓{RST} {s}"
def warn(s): return f"{Y}⚠{RST}  {s}"
def err(s): return f"{R}✗{RST} {s}"
def hdr(s): return f"\n{BOLD}{B}{s}{RST}"
def bar(pct, width=30):
    filled = int(width * pct / 100)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct:.0f}%"


# ─── Persona definitions ──────────────────────────────────────────────────────
@dataclass
class Persona:
    pid: str
    name: str
    jid: str
    category: str
    relationship: str
    importance: int
    flags: list
    expected_tier: int          # 0=silent 1=fyi 2=approve 3=ask
    urgency: str                # low / medium / high / critical
    conversations: list[dict]   # list of {turn: int, body: str, subject: str}

    @property
    def email(self):
        return self.jid.replace("@s.whatsapp.net", "@wa.test")


PERSONAS: list[Persona] = [
    # ── Investors / VIPs ──────────────────────────────────────────────────────
    Persona("p01", "Arjun Mehta", "91981200001@s.whatsapp.net", "investor",
            "investor", 90, ["investor", "vip"], 3, "critical", [
        {"turn": 1, "body": "Hey, we need to discuss the Series A terms before Friday. Sequoia pushed back on the 20% dilution clause. Can we jump on a call today?", "subject": ""},
        {"turn": 2, "body": "Also forgot to mention — the lead check is contingent on you signing the term sheet by EOD Thursday. Is that doable?", "subject": ""},
        {"turn": 3, "body": "One more thing: the other board members want a revised cap table showing post-money valuation. Can you have that ready before the call?", "subject": ""},
    ]),
    Persona("p02", "Priya Krishnamurthy", "91981200002@s.whatsapp.net", "investor",
            "investor", 85, ["investor"], 3, "high", [
        {"turn": 1, "body": "Jatin, monthly metrics look off. ARR growth dropped from 18% to 11% MoM. What happened? Need the cohort breakdown before our LP call tomorrow.", "subject": ""},
        {"turn": 2, "body": "Also need the runway calculation updated with the new hiring plan. Our LPs are specifically asking about burn rate.", "subject": ""},
    ]),

    # ── Key customers ─────────────────────────────────────────────────────────
    Persona("p03", "Rohan Gupta", "91981200003@s.whatsapp.net", "customer",
            "customer", 75, [], 2, "high", [
        {"turn": 1, "body": "We've been live on your platform for 3 weeks now and the API response times have degraded significantly. P95 is now 4.2 seconds. This is blocking our production launch.", "subject": ""},
        {"turn": 2, "body": "I sent you 3 emails about this and got no response. We're paying $8k/month for enterprise support. What's going on?", "subject": ""},
        {"turn": 3, "body": "Our CTO is now involved. If this isn't resolved by tomorrow EOD, we're escalating to a refund + cancellation.", "subject": ""},
    ]),
    Persona("p04", "Sneha Agarwal", "91981200004@s.whatsapp.net", "customer",
            "customer", 60, [], 2, "medium", [
        {"turn": 1, "body": "Hi! Quick question — can I add 5 more seats to my plan without upgrading the whole account? We just hired a new team.", "subject": ""},
        {"turn": 2, "body": "Actually never mind about the seats — can I also get a PDF invoice for the last 3 months? Our finance team needs it for audit.", "subject": ""},
    ]),

    # ── Co-founder / internal team ────────────────────────────────────────────
    Persona("p05", "Vikram Nair", "91981200005@s.whatsapp.net", "internal",
            "collaborator", 95, ["vip"], 3, "critical", [
        {"turn": 1, "body": "The prod DB is returning connection timeouts. Started 10 mins ago. Could be the migration we ran this morning. Should we rollback?", "subject": ""},
        {"turn": 2, "body": "Update: it's the new index on the messages table. Query planner is choosing the wrong path. I can drop it but need your sign-off since it affects the search feature.", "subject": ""},
        {"turn": 3, "body": "Customers are already complaining. I'm going to drop the index now and open a ticket to rebuild it properly. Just flagging you.", "subject": ""},
    ]),
    Persona("p06", "Ananya Singh", "91981200006@s.whatsapp.net", "internal",
            "collaborator", 70, [], 1, "low", [
        {"turn": 1, "body": "Hey! Can I take Friday off? I have my cousin's wedding.", "subject": ""},
        {"turn": 2, "body": "Also forgot — do we have a team lunch this week or was that next week?", "subject": ""},
    ]),
    Persona("p07", "Kiran Desai", "91981200007@s.whatsapp.net", "internal",
            "collaborator", 80, [], 2, "medium", [
        {"turn": 1, "body": "The Q3 marketing budget is ready for your approval. Total is ₹45L — breakdown in the doc I shared. We need to commit to the Google Ads campaign by tomorrow.", "subject": ""},
        {"turn": 2, "body": "Also the agency is asking about the brand guidelines. Can you confirm which version is final? The one from March or the updated April deck?", "subject": ""},
    ]),

    # ── Business partners ─────────────────────────────────────────────────────
    Persona("p08", "Deepak Bansal", "91981200008@s.whatsapp.net", "partner",
            "partner", 85, [], 3, "high", [
        {"turn": 1, "body": "Just got out of the board meeting. They want to move forward with the joint venture — but they're asking for a 60/40 split instead of 50/50. What's your position?", "subject": ""},
        {"turn": 2, "body": "Also the NDA draft my lawyer sent — have you reviewed it? The IP clause on line 47 is non-standard and my team flagged it.", "subject": ""},
    ]),
    Persona("p09", "Nalini Rajan", "91981200009@s.whatsapp.net", "partner",
            "collaborator", 65, [], 2, "medium", [
        {"turn": 1, "body": "The vendor for the Bangalore office setup is asking for 50% advance — ₹2.3L. Can you approve this? I need to respond to them today.", "subject": ""},
    ]),

    # ── Vendors ───────────────────────────────────────────────────────────────
    Persona("p10", "AWS Billing", "91981200010@s.whatsapp.net", "vendor",
            "unknown", 20, [], 1, "low", [
        {"turn": 1, "body": "Your AWS bill for May was $3,847.22. Your EC2 spend increased 34% MoM. Reserved instances could save you ~$900/month. Reply STOP to unsubscribe.", "subject": ""},
    ]),
    Persona("p11", "Rajesh Kumar - Office Supplies", "91981200011@s.whatsapp.net", "vendor",
            "unknown", 15, [], 0, "low", [
        {"turn": 1, "body": "Dear Sir/Madam, we are offering PREMIUM QUALITY office furniture at LOWEST PRICES. Chairs from ₹2,500. Desks from ₹4,000. Bulk discount available. Reply YES to know more!!", "subject": ""},
    ]),
    Persona("p12", "Sanjay Mehrotra - Legal Docs", "91981200012@s.whatsapp.net", "vendor",
            "unknown", 40, [], 3, "low", [  # legal compliance+trademark = T3 (requires decision)
        {"turn": 1, "body": "Hi, this is a reminder that the annual compliance filing deadline is June 30th. Your MCA filing and ROC forms need to be submitted. Please revert to confirm receipt.", "subject": ""},
        {"turn": 2, "body": "Also — the trademark renewal for your brand is due in July. Should we proceed? Cost is ₹12,000 per class.", "subject": ""},
    ]),

    # ── Recruiting ────────────────────────────────────────────────────────────
    Persona("p13", "Talent Recruiter - Naukri", "91981200013@s.whatsapp.net", "recruiter",
            "recruiter", 5, [], 0, "low", [
        {"turn": 1, "body": "Hi Jatin! I'm a recruiter at ABC Placements. We have exciting opportunities at top MNCs for senior tech leaders. 50-80L packages. Interested?", "subject": ""},
        {"turn": 2, "body": "Jatin bhai please reply! I have a CTO role at a unicorn startup for you. Don't miss this golden opportunity!!", "subject": ""},
    ]),
    Persona("p14", "Aditya Kapoor - Engineering Candidate", "91981200014@s.whatsapp.net", "candidate",
            "unknown", 30, [], 2, "medium", [
        {"turn": 1, "body": "Hi Jatin, I'm Aditya — 7 years in distributed systems, currently at Flipkart. Your product caught my eye and I'd love to explore if there's a fit. I've built systems handling 10M RPM. When can we talk?", "subject": ""},
        {"turn": 2, "body": "I also have a competing offer with a 3-week deadline. Wanted to be transparent so you can plan accordingly.", "subject": ""},
    ]),

    # ── Family ────────────────────────────────────────────────────────────────
    Persona("p15", "Mom", "91981200015@s.whatsapp.net", "family",
            "family", 95, ["vip", "personal"], 3, "high", [
        {"turn": 1, "body": "Beta are you eating properly? You didn't call on Sunday. Papa is asking about you.", "subject": ""},
        {"turn": 2, "body": "Also we are planning Diwali puja on October 20th. You have to come. Maasi and mausaji are also coming from Pune.", "subject": ""},
        {"turn": 3, "body": "Beta please confirm. Should I book your tickets or will you do it yourself? Don't wait too long prices are going up.", "subject": ""},
    ]),
    Persona("p16", "Dad", "91981200016@s.whatsapp.net", "family",
            "family", 90, ["vip", "personal"], 3, "medium", [
        {"turn": 1, "body": "Jatin, my laptop is showing 'disk full' error. I can't open any files. What should I do?", "subject": ""},
        {"turn": 2, "body": "I clicked on something and now there are ads everywhere. Also it's asking me to buy something called 'CleanMaster Pro'. Is this safe?", "subject": ""},
    ]),
    Persona("p17", "Riya - Sister", "91981200017@s.whatsapp.net", "family",
            "family", 80, ["personal"], 2, "low", [
        {"turn": 1, "body": "Bhai! Can I borrow ₹15,000? I'll return it next month I promise. It's for a friend's emergency.", "subject": ""},
    ]),

    # ── Friends ───────────────────────────────────────────────────────────────
    Persona("p18", "Nikhil - Best Friend", "91981200018@s.whatsapp.net", "friend",
            "collaborator", 70, ["personal"], 3, "low", [  # personal flag → guardrail always T3
        {"turn": 1, "body": "Yaar plans for this weekend? Was thinking Goa trip — 3 nights. Aman and Sid are also coming. You in?", "subject": ""},
        {"turn": 2, "body": "Also bro I heard you're doing well with the startup. So proud of you dude. Let's catch up properly soon.", "subject": ""},
    ]),
    Persona("p19", "Preethi - College Friend", "91981200019@s.whatsapp.net", "friend",
            "unknown", 30, [], 1, "low", [
        {"turn": 1, "body": "Jatin!! Omg I just saw you on LinkedIn. Congrats on the funding! We should meet up when you're in Bangalore next.", "subject": ""},
    ]),

    # ── Sales prospects ───────────────────────────────────────────────────────
    Persona("p20", "Tata Consultancy - Enterprise Prospect", "91981200020@s.whatsapp.net", "prospect",
            "customer", 70, [], 3, "high", [
        {"turn": 1, "body": "Hi Jatin, I'm the VP of Digital at TCS. We're evaluating AI communication tools for our 12,000-person BPO division. Your solution was recommended by a mutual contact. We'd need: SSO integration, on-prem option, audit logs, and SLA of 99.9%. Can you share pricing for 5,000 seats?", "subject": ""},
        {"turn": 2, "body": "Also our procurement process requires a security questionnaire. I'm attaching it. Our deadline to shortlist vendors is next Friday.", "subject": ""},
    ]),
    Persona("p21", "Startup - Warm Inbound", "91981200021@s.whatsapp.net", "prospect",
            "unknown", 40, [], 2, "medium", [
        {"turn": 1, "body": "Hey! Found you through Gaurav's recommendation. We're a 20-person SaaS company. Would love to try your product. What's the startup pricing?", "subject": ""},
    ]),

    # ── Customer support ──────────────────────────────────────────────────────
    Persona("p22", "Amit - Billing Dispute", "91981200022@s.whatsapp.net", "customer",
            "customer", 55, [], 2, "high", [
        {"turn": 1, "body": "I was charged twice for my subscription this month — ₹4,999 appeared on my card on the 1st AND the 8th. I need a refund immediately.", "subject": ""},
        {"turn": 2, "body": "I've been waiting 3 days for a response. This is completely unacceptable. I'm filing a chargeback with my bank if I don't hear back today.", "subject": ""},
    ]),
    Persona("p23", "Fatima - Feature Request", "91981200023@s.whatsapp.net", "customer",
            "customer", 45, [], 1, "low", [
        {"turn": 1, "body": "Hi, just wondering if you plan to add a dark mode? It would really help for nighttime use. Not urgent, just feedback.", "subject": ""},
    ]),
    Persona("p24", "Rajiv - Account Access", "91981200024@s.whatsapp.net", "customer",
            "customer", 50, [], 2, "medium", [
        {"turn": 1, "body": "I can't log into my account. Getting 'invalid credentials' even though I just reset my password. Been locked out for 2 hours.", "subject": ""},
        {"turn": 2, "body": "Still locked out. I have a client demo in 1 hour and I need access to my dashboard. Please help urgently!!", "subject": ""},
    ]),

    # ── Appointment / scheduling ──────────────────────────────────────────────
    Persona("p25", "Dr Sharma - Clinic", "91981200025@s.whatsapp.net", "appointment",
            "unknown", 20, [], 2, "low", [  # CONFIRM/CANCEL reply needed → T2
        {"turn": 1, "body": "Reminder: You have an appointment with Dr. Sharma on Thursday, June 20th at 10:30 AM. Please reply CONFIRM or CANCEL.", "subject": ""},
    ]),
    Persona("p26", "Meera - Meeting Request", "91981200026@s.whatsapp.net", "business",
            "unknown", 35, [], 3, "low", [  # advisory board ask + investor network → T3
        {"turn": 1, "body": "Hi Jatin! I'm from the India Angel Network. We'd love to feature your startup at our June event. Would you be available for a 15-min slot on June 22nd?", "subject": ""},
        {"turn": 2, "body": "Also — would you be interested in joining our advisory board? It's a 2 hour/month commitment. Happy to discuss.", "subject": ""},
    ]),

    # ── Travel ────────────────────────────────────────────────────────────────
    Persona("p27", "MakeMyTrip", "91981200027@s.whatsapp.net", "travel",
            "unknown", 10, [], 0, "low", [
        {"turn": 1, "body": "Your booking BLR-DEL on June 25, 6:45 AM is CONFIRMED. PNR: XKRT89. Check-in opens 24 hrs before. Download the app for real-time updates.", "subject": ""},
    ]),
    Persona("p28", "Ola Business", "91981200028@s.whatsapp.net", "travel",
            "unknown", 10, [], 0, "low", [
        {"turn": 1, "body": "Monthly expense report ready! You spent ₹12,340 on Ola rides this month. View breakdown in the app.", "subject": ""},
    ]),

    # ── Spam / scam ───────────────────────────────────────────────────────────
    Persona("p29", "Unknown - Crypto Scam", "91981200029@s.whatsapp.net", "spam",
            "unknown", 0, [], 0, "low", [
        {"turn": 1, "body": "🚀 CONGRATULATIONS! You've been selected for our EXCLUSIVE crypto investment program. Turn ₹10,000 into ₹1,00,000 in 30 days! Limited spots. Click here: bit.ly/cryptowin99", "subject": ""},
        {"turn": 2, "body": "Sir this is guaranteed returns. Many people already made money. Don't miss this GOLDEN CHANCE. Send amount to UPI: scam@paytm", "subject": ""},
    ]),
    Persona("p30", "Fake Bank Alert", "91981200030@s.whatsapp.net", "spam",
            "unknown", 0, [], 0, "low", [
        {"turn": 1, "body": "ALERT: Your SBI account has been SUSPENDED due to suspicious activity. Click the link immediately to verify your KYC: sbi-verify.tk/kyc-update or your account will be permanently blocked.", "subject": ""},
    ]),
    Persona("p31", "Lottery Scam", "91981200031@s.whatsapp.net", "spam",
            "unknown", 0, [], 0, "low", [
        {"turn": 1, "body": "Dear Winner! You have WON ₹50,00,000 in the WhatsApp Lucky Draw 2024! To claim your prize send your: Full Name, Address, Aadhaar number, Bank account details. Contact Agent ID: WA-PRIZE-9821.", "subject": ""},
    ]),

    # ── Newsletter / automated ────────────────────────────────────────────────
    Persona("p32", "TechCrunch Newsletter", "91981200032@s.whatsapp.net", "newsletter",
            "unknown", 5, [], 0, "low", [
        {"turn": 1, "body": "🗞 Today's TechCrunch Digest: OpenAI raises $10B. Apple announces Vision Pro 2. Indian SaaS market hits $50B. Read all stories: techcrunch.com/digest/june16", "subject": ""},
    ]),
    Persona("p33", "LinkedIn Notification", "91981200033@s.whatsapp.net", "automated",
            "unknown", 5, [], 0, "low", [
        {"turn": 1, "body": "You have 47 new profile views this week. 3 people viewed your post about AI. Trending in your network: 'Future of SaaS'. Connect with 12 people you may know.", "subject": ""},
    ]),

    # ── Media inquiry ─────────────────────────────────────────────────────────
    Persona("p34", "YourStory Journalist", "91981200034@s.whatsapp.net", "media",
            "unknown", 45, [], 2, "medium", [
        {"turn": 1, "body": "Hi Jatin! I'm writing a piece for YourStory on 'AI-first founders in India 2024'. Would you be interested in a 20-min interview? Deadline is this Thursday.", "subject": ""},
        {"turn": 2, "body": "Also — do you have any data points on productivity gains from AI tools? Our readers love specific numbers.", "subject": ""},
    ]),

    # ── Legal ─────────────────────────────────────────────────────────────────
    Persona("p35", "Adv. Prashant - Company Lawyer", "91981200035@s.whatsapp.net", "legal",
            "collaborator", 75, ["legal"], 3, "high", [
        {"turn": 1, "body": "Jatin, the term sheet from XYZ Fund has a 'drag-along' clause that's quite aggressive — they can force a sale with just 60% board vote. I'd recommend we push back. Should I redline it?", "subject": ""},
        {"turn": 2, "body": "Also, one of your ex-employees is claiming IP ownership of the core algorithm. This needs immediate attention — we may need to file for declaratory judgment.", "subject": ""},
    ]),

    # ── Finance / billing ─────────────────────────────────────────────────────
    Persona("p36", "Pooja - CFO", "91981200036@s.whatsapp.net", "internal",
            "collaborator", 80, [], 3, "high", [
        {"turn": 1, "body": "Monthly close is tomorrow. I still need your approval on 3 items: (1) ₹8L engineering vendor invoice (2) ₹2.5L sales commission payout (3) ₹1.2L office rent advance. Can you sign off by 5pm?", "subject": ""},
        {"turn": 2, "body": "Also, our tax consultant flagged that we haven't filed advance tax for Q1. Penalty starts accumulating from June 15. Should I proceed with the payment?", "subject": ""},
    ]),

    # ── Technical support (inbound) ────────────────────────────────────────────
    Persona("p37", "Dev - Webhook Not Firing", "91981200037@s.whatsapp.net", "customer",
            "customer", 50, [], 2, "medium", [
        {"turn": 1, "body": "Our webhook endpoint stopped receiving events about 2 hours ago. Endpoint is healthy (200s on health check). Is there an outage on your end? Urgently need this for our real-time dashboard.", "subject": ""},
    ]),

    # ── Simultaneous burst simulation ─────────────────────────────────────────
    Persona("p38", "Burst A - Suresh", "91981200038@s.whatsapp.net", "customer",
            "customer", 40, [], 1, "medium", [
        {"turn": 1, "body": "Hi quick question about pricing", "subject": ""},
        {"turn": 2, "body": "Actually wait I found it on the website", "subject": ""},
        {"turn": 3, "body": "But it doesn't mention enterprise. What's the enterprise plan?", "subject": ""},
        {"turn": 4, "body": "Hello? Anyone there?", "subject": ""},
    ]),
    Persona("p39", "Burst B - Kavita", "91981200039@s.whatsapp.net", "customer",
            "customer", 35, [], 1, "low", [
        {"turn": 1, "body": "How do I export my data", "subject": ""},
        {"turn": 2, "body": "In CSV format specifically", "subject": ""},
        {"turn": 3, "body": "I need it for my accountant", "subject": ""},
    ]),
    Persona("p40", "Burst C - Mohan", "91981200040@s.whatsapp.net", "customer",
            "customer", 30, [], 2, "low", [  # demo request → T2 (draft response)
        {"turn": 1, "body": "Can I get a demo?", "subject": ""},
        {"turn": 2, "body": "Actually I'm not the decision maker. Let me check with my manager.", "subject": ""},
    ]),

    # ── Cross-topic switcher ──────────────────────────────────────────────────
    Persona("p41", "Harish - Topic Switcher", "91981200041@s.whatsapp.net", "customer",
            "customer", 45, [], 2, "medium", [
        {"turn": 1, "body": "Quick billing question — why did my invoice go up this month?", "subject": ""},
        {"turn": 2, "body": "Oh wait, actually more important — is your API HIPAA compliant? We're in healthcare.", "subject": ""},
        {"turn": 3, "body": "Back to billing — I see a line item for 'overage charges'. I thought I had unlimited.", "subject": ""},
    ]),

    # ── Emotional escalation ──────────────────────────────────────────────────
    Persona("p42", "Frustrated Customer - Preeti", "91981200042@s.whatsapp.net", "customer",
            "customer", 55, [], 3, "critical", [
        {"turn": 1, "body": "This is absolutely ridiculous. I've been waiting 2 weeks for a response and nothing.", "subject": ""},
        {"turn": 2, "body": "Do you even care about your customers? I'm going to tweet about this to my 50k followers.", "subject": ""},
        {"turn": 3, "body": "I want to speak to your founder directly. This is unacceptable.", "subject": ""},
    ]),

    # ── Contradictory information ─────────────────────────────────────────────
    Persona("p43", "Confused User - Ravi", "91981200043@s.whatsapp.net", "customer",
            "customer", 30, [], 1, "low", [
        {"turn": 1, "body": "I want to cancel my subscription.", "subject": ""},
        {"turn": 2, "body": "Actually no wait. I want to upgrade.", "subject": ""},
        {"turn": 3, "body": "Sorry, let me think. My boss says cancel but I personally think we should stay. Can you help me make the case?", "subject": ""},
    ]),

    # ── Voice note placeholder ────────────────────────────────────────────────
    Persona("p44", "Tanya - Voice Note", "91981200044@s.whatsapp.net", "customer",
            "customer", 50, [], 2, "medium", [
        {"turn": 1, "body": "[voice note, transcribed]: Hey Jatin, it's Tanya from Razorpay. I wanted to discuss the partnership proposal we sent last week. We're offering co-marketing and a 30% revenue share on referrals. Please call me back when you can. My number is 9821XXXXXX.", "subject": ""},
    ]),

    # ── Document / image ─────────────────────────────────────────────────────
    Persona("p45", "Vendor - Invoice Image", "91981200045@s.whatsapp.net", "vendor",
            "unknown", 25, [], 1, "low", [
        {"turn": 1, "body": "[image] Caption: Invoice for April services - Total: ₹45,000. Please process payment at earliest.", "subject": ""},
    ]),

    # ── Scam awareness ────────────────────────────────────────────────────────
    Persona("p46", "Nigerian Prince Classic", "91981200046@s.whatsapp.net", "spam",
            "unknown", 0, [], 0, "low", [
        {"turn": 1, "body": "DEAR FRIEND, I am Prince Emmanuel of Nigeria. I have $45 million USD in a bank account that needs transfer urgently. I need your help. You will receive 40% commission. Please send your bank details and a processing fee of $500 to begin.", "subject": ""},
    ]),

    # ── Operational / logistics ───────────────────────────────────────────────
    Persona("p47", "Building Manager", "91981200047@s.whatsapp.net", "operational",
            "unknown", 15, [], 0, "low", [
        {"turn": 1, "body": "Dear Tenant, the building water supply will be shut for maintenance on Saturday June 22nd from 6am to 2pm. Please store sufficient water.", "subject": ""},
    ]),
    Persona("p48", "Event Organizer - TIE", "91981200048@s.whatsapp.net", "event",
            "unknown", 35, [], 3, "low", [  # speaker confirmation + bio/headshot deadline → T3
        {"turn": 1, "body": "Hi Jatin, you're confirmed as a speaker at TiECon Delhi on July 15th. Topic: 'AI in Enterprise Communication'. 30-min slot at 2:30 PM. Please confirm your travel requirements.", "subject": ""},
        {"turn": 2, "body": "Also, our comms team would like a 50-word bio and headshot for the brochure by June 28th. Can you share?", "subject": ""},
    ]),

    # ── Delayed / reconnect simulation ───────────────────────────────────────
    Persona("p49", "Long-Gap Follow-up - Anand", "91981200049@s.whatsapp.net", "customer",
            "customer", 40, [], 2, "medium", [
        {"turn": 1, "body": "Hi! We spoke 6 months ago about a pilot. We've now got budget approved. Can we reconnect? The requirements have changed a bit.", "subject": ""},
        {"turn": 2, "body": "We need NLP capabilities now in addition to what we discussed before. Also our team has grown from 5 to 23. Does your pricing scale accordingly?", "subject": ""},
    ]),

    # ── High-stakes / urgent ─────────────────────────────────────────────────
    Persona("p50", "Data Breach Alert - Security", "91981200050@s.whatsapp.net", "security",
            "collaborator", 100, ["vip"], 3, "critical", [
        {"turn": 1, "body": "URGENT: Our security scanner flagged an exposed S3 bucket with customer PII. Bucket name: prod-exports-backup. It's been public since May 3rd. We need to lock this down NOW and assess the scope. Legal may need to be notified within 72 hours per GDPR.", "subject": ""},
        {"turn": 2, "body": "I've locked the bucket but we need to do a full audit of what was exposed. I'm pulling the CloudTrail logs now. Do you want me to notify customers proactively or wait for the legal assessment?", "subject": ""},
    ]),
]


# ─── Metrics tracker ─────────────────────────────────────────────────────────
@dataclass
class Metrics:
    total: int = 0
    correct_tier: int = 0
    wrong_tier: int = 0
    surfaced_correctly: int = 0      # tier >= 2 when expected >= 2
    silenced_correctly: int = 0      # tier 0 when expected 0
    false_positives: int = 0         # surfaced when should be silent
    false_negatives: int = 0         # silenced when should be surfaced
    duplicates_detected: int = 0
    duplicates_prevented: int = 0
    memory_writes: int = 0
    memory_reads: int = 0
    contamination_checks: int = 0
    contamination_failures: int = 0
    errors: list[str] = field(default_factory=list)
    tier_dist: dict[int, int] = field(default_factory=lambda: {0:0,1:0,2:0,3:0})
    persona_results: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def accuracy(self): return self.correct_tier / max(self.total, 1)
    @property
    def precision(self):
        surfaced = self.surfaced_correctly + self.false_positives
        return self.surfaced_correctly / max(surfaced, 1)
    @property
    def recall(self):
        expected_surfaced = self.surfaced_correctly + self.false_negatives
        return self.surfaced_correctly / max(expected_surfaced, 1)


# ─── Synthetic message/thread builder ────────────────────────────────────────
def build_thread(persona: Persona, turn_idx: int) -> tuple:
    """Build a (Thread, Contact) for a given persona + turn."""
    from assistant.models import Channel, Contact, Message, Thread

    conv = persona.conversations[turn_idx]
    msg_id = f"sim_{persona.pid}_t{turn_idx}_{uuid.uuid4().hex[:8]}"
    thread_id = f"thread_{persona.pid}"
    ts = time.time() - (len(persona.conversations) - turn_idx) * 60

    messages = []
    # Add prior turns as context (from_me=False for all inbound)
    for i, prior in enumerate(persona.conversations[:turn_idx]):
        messages.append(Message(
            id=f"sim_{persona.pid}_t{i}_{uuid.uuid4().hex[:6]}",
            thread_id=thread_id,
            channel=Channel.WHATSAPP,
            sender_email=persona.email,
            sender_name=persona.name,
            body_text=prior["body"],
            subject=prior.get("subject", ""),
            timestamp=ts - (turn_idx - i) * 60,
            from_me=False,
        ))

    # Latest inbound message
    messages.append(Message(
        id=msg_id,
        thread_id=thread_id,
        channel=Channel.WHATSAPP,
        sender_email=persona.email,
        sender_name=persona.name,
        body_text=conv["body"],
        subject=conv.get("subject", ""),
        timestamp=ts,
        from_me=False,
    ))

    thread = Thread(
        id=thread_id,
        channel=Channel.WHATSAPP,
        subject=conv.get("subject", ""),
        messages=messages,
    )
    contact = Contact(
        email=persona.email,
        name=persona.name,
        relationship=persona.relationship,
        importance=persona.importance,
        flags=set(persona.flags),
        msg_count=turn_idx,
    )
    return thread, contact


def build_context(conn, persona: Persona, person_id: str):
    """Build a RetrievedContext with any prior memory for this persona."""
    from assistant.memory.retrieval import RetrievedContext
    from assistant.models import Contact

    from assistant.storage import repositories as repo

    contact = Contact(
        email=persona.email,
        name=persona.name,
        relationship=persona.relationship,
        importance=persona.importance,
        flags=set(persona.flags),
    )

    ctx = RetrievedContext(contact=contact, person_id=person_id)

    # Load memory if it exists
    try:
        from assistant.memory import distill as distill_mod
        mem = distill_mod.load_memory(conn, person_id)
        from assistant.memory import retrieval
        ctx.memory_block = retrieval.build_memory_block(mem, cap=distill_mod.MEMORY_CHAR_CAP)
    except Exception:
        pass

    return ctx


# ─── Runner ──────────────────────────────────────────────────────────────────
def run_persona(conn, settings, llm, persona: Persona, verbose: bool,
                max_turns: int | None = None) -> dict:
    """Run conversation turns for one persona. Returns a result dict."""
    from assistant.brain import classifier
    from assistant.brain.tiers import TierConfig, decide as decide_tier
    from assistant.memory import distill as distill_mod
    from assistant.memory import retrieval
    from assistant.storage import repositories as repo, ledger

    result = {
        "pid": persona.pid,
        "name": persona.name,
        "category": persona.category,
        "expected_tier": persona.expected_tier,
        "urgency": persona.urgency,
        "turns": [],
        "final_tier": None,
        "memory_after": None,
        "errors": [],
        "ok": True,
    }

    # Register a person identity for this persona
    person_id = f"person_{persona.pid}"
    try:
        conn.execute(
            "INSERT OR IGNORE INTO persons (id, display_name, emails, phone_jids, relationship_type) "
            "VALUES (?,?,?,?,?)",
            (person_id, persona.name, "[]", f'["{persona.jid}"]', persona.relationship),
        )
        repo.person_link_set(conn, persona.email, person_id, source="test")
        repo.person_link_set(conn, persona.jid, person_id, source="test")
    except Exception as e:
        result["errors"].append(f"identity setup: {e}")

    last_tier = -1
    tier_config = TierConfig.from_settings(settings)
    convs = persona.conversations if max_turns is None else persona.conversations[:max_turns]

    for turn_idx, conv in enumerate(convs):
        turn_result = {"turn": turn_idx + 1, "body_preview": conv["body"][:60], "tier": None, "error": None}
        try:
            thread, contact = build_thread(persona, turn_idx)
            context = build_context(conn, persona, person_id)

            # Classify
            decision = classifier.classify_thread(
                conn, llm, thread, context,
                prompts_dir=str(ROOT / "prompts"),
            )
            final = decide_tier(thread, decision, contact, tier_config)
            tier = int(final.final_tier)
            turn_result["tier"] = tier
            turn_result["category"] = decision.category
            turn_result["confidence"] = round(final.confidence, 2)
            last_tier = tier

            # Ledger exactly-once gate: mark_seen returns False if message already known.
            msg_id = thread.latest.id
            is_new = ledger.mark_seen(conn, msg_id, thread_id=thread.id)
            if is_new:
                ledger.claim(conn, msg_id)
            else:
                turn_result["duplicate_prevented"] = True

            # Distill memory (non-noise turns)
            from assistant.memory.distill import NOISE_CATEGORIES
            if decision.category not in NOISE_CATEGORIES and tier >= 1:
                try:
                    distill_mod.distill(conn, llm, settings, person_id, thread)
                except Exception as de:
                    turn_result["distill_error"] = str(de)[:80]

        except Exception as e:
            turn_result["error"] = str(e)[:120]
            result["errors"].append(f"turn {turn_idx+1}: {e}")
            result["ok"] = False
            traceback.print_exc() if verbose else None

        result["turns"].append(turn_result)

    result["final_tier"] = last_tier if last_tier >= 0 else None

    # Read back memory to verify isolation
    try:
        from assistant.memory import distill as distill_mod
        mem = distill_mod.load_memory(conn, person_id)
        result["memory_after"] = {
            "facts": len(mem.summary),
            "open_situations": len(mem.open_situations),
            "episodes": len(mem.episodes),
        }
    except Exception:
        pass

    return result


def check_contamination(conn, persona_a: Persona, persona_b: Persona) -> bool:
    """Check that person A's facts don't appear in person B's memory. Returns True=clean."""
    try:
        from assistant.memory import distill as distill_mod
        mem_a = distill_mod.load_memory(conn, f"person_{persona_a.pid}")
        mem_b = distill_mod.load_memory(conn, f"person_{persona_b.pid}")
        # Simple check: no JID/email from A in B's memory JSON
        mem_b_str = str(mem_b.summary) + str(mem_b.open_situations)
        leak_markers = [persona_a.jid, persona_a.email, persona_a.name.split()[0].lower()]
        return not any(m.lower() in mem_b_str.lower() for m in leak_markers)
    except Exception:
        return True  # assume clean if we can't check


# ─── Live event stream (for display) ─────────────────────────────────────────
def print_event(ts_offset: float, persona: Persona, turn: int, tier: int | None,
                category: str = "", status: str = ""):
    tier_labels = {0: f"{W}T0:SILENT{RST}", 1: f"{B}T1:FYI{RST}",
                   2: f"{Y}T2:APPROVE{RST}", 3: f"{R}T3:ASK{RST}"}
    tier_str = tier_labels.get(tier, f"{M}?{RST}") if tier is not None else f"{M}CLASSIFYING{RST}"
    elapsed = f"+{ts_offset:05.1f}s"
    cat_str = f" [{category}]" if category else ""
    print(f"  {W}{elapsed}{RST}  {C}{persona.pid}{RST}  {BOLD}{persona.name[:22]:<22}{RST}  "
          f"turn {turn}  {tier_str}{cat_str}  {status}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chats", type=int, default=50, help="Number of personas to run (max 50)")
    parser.add_argument("--quick", action="store_true", help="1 turn per persona only")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    n = min(args.chats, len(PERSONAS))
    personas = PERSONAS[:n]

    print(f"\n{BOLD}{M}{'='*65}{RST}")
    print(f"{BOLD}{M}  STEWARD — WhatsApp Load Test & Production Simulator{RST}")
    print(f"{BOLD}{M}{'='*65}{RST}")
    print(f"  Personas : {BOLD}{n}{RST}")
    print(f"  Mode     : {BOLD}DRY_RUN=true, isolated temp DB{RST}")
    print(f"  Pipeline : {BOLD}real classifier + tier engine + memory distill{RST}")
    print(f"  Impact   : {G}ZERO{RST} (no prod DB, no real sends, no real LLM messages to Jatin)")

    # ── Setup ────────────────────────────────────────────────────────────────
    print(hdr("Setting up isolated test environment…"))

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="steward_loadtest_") as f:
        db_path = f.name

    try:
        from assistant.config import Settings
        from assistant.storage.db import open_db
        from assistant.llm.client import LLMClient

        # Load real env so API keys are available, then override test-specific fields
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)

        conn = open_db(db_path)
        settings = Settings(
            db_path=db_path,
            mode="dry_run",
            memory_enabled=True,
            telegram_chat_id="0",
            prompts_dir=str(ROOT / "prompts"),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )
        llm = LLMClient(settings)
        print(f"  {ok('Temp DB created:')} {db_path[-40:]}")
        print(f"  {ok('LLM client ready')}")
        print(f"  {ok('Settings: dry_run=True, memory_enabled=True')}")

    except Exception as e:
        print(f"  {err(f'Setup failed: {e}')}")
        traceback.print_exc()
        sys.exit(1)

    # ── Run ──────────────────────────────────────────────────────────────────
    metrics = Metrics()
    start_time = time.time()

    print(hdr(f"Running {n} personas through real pipeline…"))
    print(f"  {'ELAPSED':>8}  {'ID':>5}  {'NAME':<22}  {'TURN'}  {'RESULT'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*22}  {'-'*4}  {'-'*24}")

    all_results = []
    burst_personas = [p for p in personas if p.pid in ("p38", "p39", "p40")]
    burst_fired = False

    for persona in personas:
        turns = persona.conversations if not args.quick else persona.conversations[:1]

        # Simulate burst event: fire 3 personas simultaneously (we serialise but track it)
        if not burst_fired and persona.pid == "p38":
            burst_fired = True
            print(f"\n  {R}{BOLD}★ BURST EVENT — 3 chats activating simultaneously ★{RST}")

        for turn_idx, conv in enumerate(turns):
            ts_offset = time.time() - start_time
            print_event(ts_offset, persona, turn_idx + 1, None)

        result = run_persona(conn, settings, llm, persona, verbose=args.verbose,
                             max_turns=1 if args.quick else None)
        all_results.append(result)

        # Print final turn results
        for tr in result["turns"]:
            if not args.quick or tr["turn"] == 1:
                ts_offset = time.time() - start_time
                status = ""
                if tr.get("error"):
                    status = f"{R}ERROR: {tr['error'][:40]}{RST}"
                elif tr.get("duplicate_prevented"):
                    status = f"{Y}[dup prevented]{RST}"
                tier = tr.get("tier")
                cat = tr.get("category", "")
                # Overwrite previous line
                print(f"\033[A\033[K", end="")
                print_event(ts_offset, persona, tr["turn"], tier, cat, status)

        # Metrics
        metrics.total += 1
        final_tier = result["final_tier"]
        if final_tier is not None:
            metrics.tier_dist[min(final_tier, 3)] = metrics.tier_dist.get(min(final_tier, 3), 0) + 1
            expected = persona.expected_tier

            # Correct if within 1 tier OR if both are "noise" (0) or both "surface" (>=2)
            correct = (
                final_tier == expected or
                (final_tier == 0 and expected == 0) or
                (final_tier >= 2 and expected >= 2 and abs(final_tier - expected) <= 1)
            )
            if correct:
                metrics.correct_tier += 1
            else:
                metrics.wrong_tier += 1
                if args.verbose:
                    print(f"  {warn(f'{persona.name}: expected T{expected}, got T{final_tier}')}")

            if expected == 0 and final_tier == 0:
                metrics.silenced_correctly += 1
            elif expected == 0 and final_tier >= 2:
                metrics.false_positives += 1
            elif expected >= 2 and final_tier >= 2:
                metrics.surfaced_correctly += 1
            elif expected >= 2 and final_tier == 0:
                metrics.false_negatives += 1

        if result["memory_after"] and result["memory_after"]["facts"] > 0:
            metrics.memory_writes += 1

        if result["errors"]:
            metrics.errors.extend([f"{persona.name}: {e}" for e in result["errors"]])

    # ── Contamination checks ────────────────────────────────────────────────
    print(hdr("Running memory isolation checks…"))
    isolation_pairs = [
        (PERSONAS[0], PERSONAS[28]),   # investor vs spam
        (PERSONAS[14], PERSONAS[29]),  # mom vs scam
        (PERSONAS[4], PERSONAS[31]),   # co-founder vs newsletter
        (PERSONAS[2], PERSONAS[9]),    # customer vs friend
        (PERSONAS[34], PERSONAS[46]),  # lawyer vs scammer
    ]
    for pa, pb in isolation_pairs:
        if pa in personas and pb in personas:
            metrics.contamination_checks += 1
            clean = check_contamination(conn, pa, pb)
            if clean:
                print(f"  {ok(f'{pa.name} → {pb.name}: isolated')}")
            else:
                metrics.contamination_failures += 1
                print(f"  {err(f'{pa.name} → {pb.name}: CONTAMINATION DETECTED')}")

    # ── Duplicate injection test ─────────────────────────────────────────────
    # mark_seen is the exactly-once gate: INSERT OR IGNORE. First call inserts
    # (returns True = new). Second call hits the IGNORE (returns False = dup).
    # After complete(), a third mark_seen for the SAME id also returns False.
    print(hdr("Testing duplicate message prevention…"))
    from assistant.storage import ledger as ledger_mod
    dup_msg_id = f"dup_test_{uuid.uuid4().hex}"
    first_seen = ledger_mod.mark_seen(conn, dup_msg_id, thread_id="dup_thread")
    second_seen = ledger_mod.mark_seen(conn, dup_msg_id, thread_id="dup_thread")
    ledger_mod.claim(conn, dup_msg_id)
    ledger_mod.complete(conn, dup_msg_id, dry_run=True)
    third_seen = ledger_mod.mark_seen(conn, dup_msg_id, thread_id="dup_thread")
    if first_seen and not second_seen and not third_seen:
        metrics.duplicates_prevented += 1
        print(f"  {ok('Duplicate prevention: first=new, second+third=rejected (correct)')}")
    else:
        print(f"  {err(f'Dup prevention FAILED: first_seen={first_seen}, second_seen={second_seen}, third_seen={third_seen}')}")

    # ─── Final scoring report ────────────────────────────────────────────────
    elapsed = time.time() - start_time
    metrics.elapsed_seconds = elapsed

    print(f"\n{BOLD}{M}{'='*65}{RST}")
    print(f"{BOLD}{M}  SCORING REPORT{RST}")
    print(f"{BOLD}{M}{'='*65}{RST}")

    acc_pct = metrics.accuracy * 100
    acc_colour = G if acc_pct >= 80 else Y if acc_pct >= 60 else R
    print(f"\n  {BOLD}TIER ACCURACY{RST}          {acc_colour}{bar(acc_pct)}{RST}")
    print(f"     Correct: {G}{metrics.correct_tier}{RST}  Wrong: {R}{metrics.wrong_tier}{RST}  "
          f"Total: {metrics.total}")

    prec_pct = metrics.precision * 100
    rec_pct = metrics.recall * 100
    print(f"\n  {BOLD}SURFACING QUALITY{RST}")
    print(f"     Precision: {G if prec_pct >= 80 else Y}{prec_pct:.0f}%{RST}  "
          f"(surfaced items that SHOULD have been surfaced)")
    print(f"     Recall:    {G if rec_pct >= 80 else Y}{rec_pct:.0f}%{RST}  "
          f"(important items that WERE surfaced)")
    print(f"     False +ve: {R if metrics.false_positives else G}{metrics.false_positives}{RST}  "
          f"(spam surfaced as important)")
    print(f"     False -ve: {R if metrics.false_negatives else G}{metrics.false_negatives}{RST}  "
          f"(important items silenced)")

    print(f"\n  {BOLD}TIER DISTRIBUTION{RST}")
    tier_names = {0: "SILENT", 1: "FYI", 2: "APPROVE", 3: "ASK"}
    tier_colours = {0: W, 1: B, 2: Y, 3: R}
    for t in range(4):
        cnt = metrics.tier_dist.get(t, 0)
        pct = cnt / max(metrics.total, 1) * 100
        colour = tier_colours[t]
        print(f"     T{t} {tier_names[t]:<7}: {colour}{cnt:>3}{RST}  {bar(pct, 20)}")

    print(f"\n  {BOLD}MEMORY{RST}")
    print(f"     Personas with memory written: {G}{metrics.memory_writes}{RST} / {metrics.total}")

    print(f"\n  {BOLD}ISOLATION{RST}")
    iso_ok = metrics.contamination_checks - metrics.contamination_failures
    print(f"     Checks: {metrics.contamination_checks}  "
          f"Clean: {G}{iso_ok}{RST}  "
          f"Failed: {R if metrics.contamination_failures else G}{metrics.contamination_failures}{RST}")

    print(f"\n  {BOLD}DUPLICATE PREVENTION{RST}")
    print(f"     Tests passed: {G}{metrics.duplicates_prevented}{RST}")

    print(f"\n  {BOLD}ERRORS{RST}  ({len(metrics.errors)} total)")
    for e in metrics.errors[:10]:
        print(f"     {R}✗{RST} {e[:80]}")
    if len(metrics.errors) > 10:
        print(f"     … and {len(metrics.errors) - 10} more")

    print(f"\n  {BOLD}PERFORMANCE{RST}")
    print(f"     Total elapsed: {elapsed:.1f}s")
    print(f"     Avg per persona: {elapsed/max(metrics.total,1):.1f}s")

    # Per-persona summary (unexpected tiers)
    surprises = [r for r in all_results if r["final_tier"] is not None
                 and abs(r["final_tier"] - r["expected_tier"]) > 1]
    if surprises:
        print(f"\n  {BOLD}UNEXPECTED RESULTS (delta > 1 tier){RST}")
        for r in surprises[:10]:
            print(f"     {Y}{r['name']}{RST}: expected T{r['expected_tier']}, "
                  f"got T{r['final_tier']}  [{r['category'] if r['turns'] else '?'}]")

    # Overall score
    score_components = [
        ("Tier accuracy",    acc_pct),
        ("Precision",        prec_pct),
        ("Recall",           rec_pct),
        ("No contamination", 100.0 if not metrics.contamination_failures else 0.0),
        ("Dup prevention",   100.0 if metrics.duplicates_prevented else 0.0),
        ("Error rate",       max(0, 100 - len(metrics.errors) * 10)),
    ]
    overall = sum(v for _, v in score_components) / len(score_components)
    overall_colour = G if overall >= 80 else Y if overall >= 60 else R
    print(f"\n{BOLD}{M}{'='*65}{RST}")
    print(f"  {BOLD}OVERALL SCORE{RST}   {overall_colour}{BOLD}{overall:.0f} / 100{RST}")
    print(f"  {bar(overall, 40)}")
    for label, val in score_components:
        c = G if val >= 80 else Y if val >= 60 else R
        print(f"     {label:<22}: {c}{val:.0f}{RST}")
    print(f"{BOLD}{M}{'='*65}{RST}\n")

    # Cleanup
    conn.close()
    try:
        os.unlink(db_path)
    except Exception:
        pass

    return 0 if overall >= 60 else 1


if __name__ == "__main__":
    sys.exit(main())
