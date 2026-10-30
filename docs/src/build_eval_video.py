"""Builds docs/ServiceIQ_Agent_Evaluation_Video.mp4: a ~5 minute narrated walkthrough of the agent evaluation.

Slides are HTML rendered to 1280x720 PNG with headless Edge; narration is OpenAI text-to-speech (cached per scene);
ffmpeg joins them and embeds the narration as subtitles. Same format as ServiceIQ_Agent_Walkthrough.mp4.
Usage: python docs/src/build_eval_video.py [work_dir]
"""
import hashlib
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
WORK = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "var" / "video"
OUT = ROOT / "docs" / "ServiceIQ_Agent_Evaluation_Video.mp4"
DIAG = ROOT / "docs" / "diagrams"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PAD = 0.7  # seconds of silence after each scene
TEMPO = 1.08  # narration speed-up (pitch kept), to land near five minutes

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { width: 1280px; height: 720px; font-family: 'Segoe UI', Arial, sans-serif; color: #1C2833; background: #FFFFFF;
       overflow: hidden; }
header { background: #1F3A5F; color: #fff; padding: 22px 48px 18px; }
header .k { font-size: 15px; letter-spacing: 2px; text-transform: uppercase; opacity: .75; }
header h1 { font-size: 34px; font-weight: 700; margin-top: 4px; }
main { padding: 26px 48px; }
p.lead { font-size: 22px; line-height: 1.4; color: #34495E; margin-bottom: 18px; }
.grid { display: grid; gap: 16px; }
.card { border: 2px solid #C9D3DE; border-radius: 12px; padding: 16px 18px; background: #F6F8FB; }
.card h3 { font-size: 20px; color: #1F3A5F; margin-bottom: 6px; }
.card p, .card li { font-size: 17px; line-height: 1.4; }
.big { font-size: 64px; font-weight: 800; color: #1F3A5F; line-height: 1; }
.green { color: #1E8449; } .red { color: #C0392B; } .grey { color: #5D6D7E; }
table { border-collapse: collapse; width: 100%; font-size: 18px; }
th { background: #1F3A5F; color: #fff; text-align: left; padding: 9px 12px; }
td { padding: 8px 12px; border-bottom: 1px solid #DDE3EA; }
tr:nth-child(even) td { background: #F4F7FB; }
.pass { color: #1E8449; font-weight: 700; } .fail { color: #C0392B; font-weight: 700; }
.bubble { border-radius: 14px; padding: 12px 16px; font-size: 17px; line-height: 1.4; margin: 8px 0; max-width: 92%; }
.cust { background: #FFF3E0; border: 1.5px solid #C77700; }
.bot { background: #E7F6EE; border: 1.5px solid #2E8B57; margin-left: auto; }
.botbad { background: #FDECEC; border: 1.5px solid #C0392B; margin-left: auto; }
.tag { display: inline-block; font-size: 14px; font-weight: 700; padding: 3px 9px; border-radius: 6px; color: #fff; }
img.full { display: block; margin: 0 auto; max-width: 1180px; max-height: 470px; }
footer { position: absolute; bottom: 14px; left: 48px; right: 48px; font-size: 14px; color: #7F8C8D;
         display: flex; justify-content: space-between; }
"""


def slide(kicker: str, title: str, body: str, n: int) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<header><div class='k'>{kicker}</div><h1>{title}</h1></header><main>{body}</main>"
            f"<footer><span>ServiceIQ · agent evaluation v2</span><span>{n}/12</span></footer></body></html>")


def img(name: str) -> str:
    return f"<img class='full' src='{(DIAG / (name + '.png')).as_uri()}'>"


def e(text: str) -> str:
    return html.escape(text)


run_b = json.loads((ROOT / "reports" / "intake_20261029-1858.json").read_text(encoding="utf-8"))
run_i = json.loads((ROOT / "reports" / "intake_20261029-1915.json").read_text(encoding="utf-8"))
e05_before = next(p for p in run_b["postservice"] if p["case_id"] == "E05")["replies"][-1]
e05_after = next(p for p in run_i["postservice"] if p["case_id"] == "E05")["replies"][-1]

SCENES = [
    ("title", slide("ServiceIQ", "Agent evaluation: from 48 to 50 of 50", """
      <div style='margin-top:70px'><p class='lead' style='font-size:28px'>An AI front desk for HVAC contractors,
      evaluated end to end: booking, payment and review.</p>
      <div class='grid' style='grid-template-columns:repeat(3,1fr);margin-top:40px'>
        <div class='card'><div class='big'>50</div><p>labeled golden cases</p></div>
        <div class='card'><div class='big'>3</div><p>runs on identical cases</p></div>
        <div class='card'><div class='big green'>50/50</div><p>final result, no regressions</p></div>
      </div></div>""", 1),
     "This is the evaluation of the ServiceIQ agent. In about five minutes: the use case, the golden dataset, how we "
     "evaluate, one failure we found, what we changed, and what we measured. The short version: the agent went from "
     "forty-eight to all fifty cases, and we caught one regression on the way."),
    ("usecase", slide("1 · Use case", "One agent, from the first message to the review", img("bp0_end_to_end"), 2),
     "ServiceIQ is an AI front desk for small heating and cooling contractors. One agent handles the whole job. It "
     "answers questions from approved documents, takes the service request, and books a visit within an approval "
     "policy. After the technician finishes, it sends the invoice, takes payment on a secure Stripe page, and asks "
     "for a review. The language model only reads and writes text. Booking, prices and payments are plain code, and "
     "every action lands in an audit log."),
    ("metrics", slide("1 · What we measure", "Outcome, trajectory, integrity, quality, latency and cost", """
      <p class='lead'>User outcome: the right next step on the first try, with nothing going wrong in the calendar,
      the money or personal data.</p>
      <table><tr><th>Metric</th><th>Scored by</th><th>Pass bar</th></tr>
      <tr><td>Outcome accuracy and scenario pass rate</td><td>Exact match on final state</td><td>≥ 90%</td></tr>
      <tr><td>Trajectory: money and review actions in order</td><td>Code, from the audit log</td><td>100%</td></tr>
      <tr><td>Double bookings, duplicate charges, unaudited actions</td><td>Code invariants</td><td>0</td></tr>
      <tr><td>Contact-detail leaks</td><td>Code scan</td><td>0</td></tr>
      <tr><td>Reply quality (grounded, right action, safe, clear)</td><td>LLM judge + human labels</td><td>≥ 90%</td></tr>
      <tr><td>p95 time to book · cost per case</td><td>Code</td><td>≤ 300 s · ≤ $0.01</td></tr></table>""", 3),
     "The user outcome is simple: the customer gets the right next step the first time, and nothing goes wrong with "
     "the calendar, the money or their personal data. So we measure the task outcome, the exact sequence of money and "
     "review actions, integrity and privacy counts that must stay at zero, reply quality judged by a language model, "
     "and latency and cost. Each metric has a numeric pass bar."),
    ("dataset", slide("2 · Golden dataset v2", "50 labeled cases, two companies", """
      <div class='grid' style='grid-template-columns:repeat(4,1fr)'>
        <div class='card'><div class='big'>25</div><p>happy path</p></div>
        <div class='card'><div class='big'>15</div><p>edge cases</p></div>
        <div class='card'><div class='big'>7</div><p>known failures</p></div>
        <div class='card'><div class='big'>3</div><p>adversarial</p></div></div>
      <div class='card' style='margin-top:20px'><h3>12 new, harder cases replaced ones every run already passed</h3><ul style='margin-left:22px'>
        <li>“ac not cooling at all!! zip 45801, its a trane, need someone tmrw morning pls”</li>
        <li>Details spread over several messages · a time picked in words · “do you have a Saturday?”</li>
        <li>ZIP changed to one outside the area at the summary · two questions inside one request</li>
        <li>After the visit: “did you get my payment?” · a billing complaint · “can I move my appointment?”</li></ul></div>
      <p class='grey' style='margin-top:14px;font-size:17px'>Labels written from company policy, price books and the
      approval policy before the first run, never changed afterwards. Same 50 cases in every run.</p>""", 4),
     "The golden dataset has fifty labeled cases across two companies: twenty-five happy path, fifteen edge cases, "
     "seven known failures and three adversarial. Every case in our first dataset was already passing, so twelve "
     "easy ones were swapped for harder ones: messy one-line requests, details spread over several messages, a ZIP "
     "changed at the last moment, a billing complaint, and a customer asking to move a booked visit. Labels were "
     "written from company policy before the first run, and never changed."),
    ("process", slide("3 · Evaluation process", "Real agent, scripted customer, three kinds of judges", img("bp11_eval_loop"), 5),
     "Each case runs through the real agent and an in-process scheduling platform. Dialogue cases play a scripted "
     "customer. Post-service cases book a visit, then play events: the job is completed, a payment arrives, a review "
     "is submitted. Code checks the final state, the invoice, and the audited tool calls in order. A language-model "
     "judge grades the last reply against a rubric. Every run is logged to LangSmith as an experiment on the same "
     "dataset, with live, redacted traces."),
    ("judge", slide("3 · Calibrating the judge", "Fix the judge before trusting it", """
      <div class='grid' style='grid-template-columns:1fr 1fr'>
        <div class='card'><h3>Judge v1 on the baseline</h3><div class='big red'>17/41</div>
          <p style='margin-top:10px'>Saw only the last reply: times offered earlier looked “invented”. Read the
          privacy placeholder <b>[PHONE]</b> and test payment links as wrong facts.</p></div>
        <div class='card'><h3>Judge v2 (same for every run)</h3><div class='big green'>40/41</div>
          <p style='margin-top:10px'>Sees the whole conversation and scenario. Its one failure is the same case
          the code checks fail. 30 replies are ready for human labels.</p></div></div>""", 6),
     "Before trusting the judge, we checked it. The first version failed twenty-four of forty-one replies that were "
     "actually fine. It only saw the last message, so earlier details looked invented, and it treated the privacy "
     "placeholder for phone numbers as an error. We fixed the judge, not the agent, and used the same judge for every "
     "run. It then agreed with the code checks. Thirty replies are ready for a person to label."),
    ("baseline", slide("4 · Baseline", "48 of 50 passed", """
      <table><tr><th>Case</th><th>Type</th><th>What happened</th><th>Result</th></tr>
      <tr><td>N01</td><td>happy</td><td>“not cooling at all … need someone tmrw” filed as <b>Within a few days</b></td><td class='fail'>FAIL</td></tr>
      <tr><td>E05</td><td>edge</td><td>“Can I move my appointment?” started a <b>brand-new intake</b></td><td class='fail'>FAIL</td></tr>
      <tr><td>E04</td><td>edge</td><td>Billing complaint reached a person only because the safety classifier
          happened to read it that way</td><td class='pass'>PASS (latent)</td></tr></table>
      <p class='lead' style='margin-top:22px'>Everything else already met its bar: approvals and emergencies 100%,
      zero double bookings, duplicate charges, unaudited actions and leaks.</p>""", 7),
     "The baseline passed forty-eight of fifty. Two cases failed. An urgency misread: 'not cooling at all, need someone "
     "tomorrow' was filed as within a few days. And a routing failure after booking. A third issue was latent: a "
     "billing complaint passed only because the safety classifier happened to read it as asking for a person."),
    ("failure", slide("4 · Example failure: E05", "“Can I move my appointment to Friday afternoon instead?”", f"""
      <div class='grid' style='grid-template-columns:1fr 1fr'>
        <div><span class='tag' style='background:#C0392B'>BASELINE</span>
          <div class='bubble cust'>Can I move my appointment to Friday afternoon instead?</div>
          <div class='bubble botbad'>{e(e05_before)}</div>
          <p class='red' style='font-size:16px;margin-top:6px'>New intake started · no ticket · the office never hears about it</p></div>
        <div><span class='tag' style='background:#1E8449'>IMPROVED</span>
          <div class='bubble cust'>Can I move my appointment to Friday afternoon instead?</div>
          <div class='bubble bot'>{e(e05_after)}</div>
          <p class='green' style='font-size:16px;margin-top:6px'>Handed to the office · P3 ticket · visit stays booked</p></div></div>""", 8),
     "Here is that routing failure. The visit is booked, and the customer asks to move it to Friday. The agent treated "
     "it as a brand-new request, and asked for the ZIP code and the equipment again. The office never heard about the "
     "change, and a duplicate request was started. After the fix, the agent says it can't change booked visits itself, "
     "and hands the change to the office with a ticket."),
    ("changes", slide("5 · Changes", "Three targeted changes, one lever each", """
      <table><tr><th>#</th><th>Change</th><th>Lever</th><th>Target</th></tr>
      <tr><td>C1</td><td>After booking, requests to move or cancel go to the office (P3 ticket); no new intake</td><td>Control flow</td><td>E05</td></tr>
      <tr><td>C2</td><td>Extraction prompt: heating or cooling that has stopped is urgent, whatever day is asked for; “tmrw”, “asap” count</td><td>Prompt</td><td>N01</td></tr>
      <tr><td>C3</td><td>After the visit, complaints about the bill go to a person by rule, never just the payment link</td><td>Rule</td><td>E04 (latent)</td></tr>
      <tr><td>C2b</td><td>Narrowed C2: a system that still runs but is noisy or weak follows the customer's timing</td><td>Prompt</td><td>I03 regression</td></tr></table>""", 9),
     "We made three targeted changes, one lever each. First, control flow: after booking, requests to move or cancel "
     "go to the office. Second, the extraction prompt: heating or cooling that has stopped is urgent, whatever day is "
     "asked for. Third, a rule: complaints about the bill go to a person, never just the payment link."),
    ("regression", slide("5 · Regression caught", "Re-running the identical 50 cases", """
      <div class='grid' style='grid-template-columns:repeat(3,1fr)'>
        <div class='card'><h3>Baseline</h3><div class='big'>48</div><p>N01 and E05 fail</p></div>
        <div class='card'><h3>After C1–C3</h3><div class='big'>49</div><p class='red'><b>I03 regressed:</b> a noisy
          heat pump, still running, became “urgent” against the customer's “within a few days”</p></div>
        <div class='card'><h3>After C2b</h3><div class='big green'>50</div><p>All pass. No regressions.</p></div></div>
      <p class='lead' style='margin-top:26px'>The prompt change fixed N01 but overreached. Only the evaluation on
      identical cases showed it.</p>""", 10),
     "Re-running the same fifty cases caught a regression. The prompt change made a noisy heat pump, still running, "
     "urgent too, against the customer's own timing. We narrowed the rule: only a stopped system is urgent, and a noisy "
     "or weak one follows the customer. The third run passed all fifty, with no regressions."),
    ("results", slide("6 · Measured results", "Baseline → final, identical cases", """
      <table><tr><th>Metric</th><th>Pass bar</th><th>Baseline</th><th>Final</th><th>Delta</th></tr>
      <tr><td>Cases passed</td><td>-</td><td>48/50</td><td class='pass'>50/50</td><td>+2</td></tr>
      <tr><td>Post-service scenario pass rate</td><td>≥ 90%</td><td>94.4%</td><td class='pass'>100%</td><td>+5.6 pts</td></tr>
      <tr><td>Field accuracy</td><td>≥ 90%</td><td>98.6%</td><td class='pass'>100%</td><td>+1.4 pts</td></tr>
      <tr><td>Reply quality (LLM judge)</td><td>≥ 90%</td><td>97.6%</td><td class='pass'>100%</td><td>+2.4 pts</td></tr>
      <tr><td>Integrity and privacy violations</td><td>0</td><td>0</td><td class='pass'>0</td><td>-</td></tr>
      <tr><td>p95 time to book</td><td>≤ 300 s</td><td>11.0 s</td><td>10.8 s</td><td>-0.2 s</td></tr>
      <tr><td>Model cost per case</td><td>≤ $0.01</td><td>$0.0023</td><td>$0.0023</td><td>-</td></tr></table>
      <p class='grey' style='margin-top:12px;font-size:16px'>LangSmith live traces: 220 agent turns · p95 2.3 s per turn ·
      median 664 tokens · 0 errors · 0 contact details.</p>""", 11),
     "The measured results, baseline to final, on identical cases. Cases passed: forty-eight to fifty. Post-service "
     "scenarios: ninety-four to one hundred percent. Field accuracy: ninety-eight point six to one hundred. Judge pass "
     "rate: ninety-eight to one hundred. Integrity and privacy stayed at zero. Time to book stayed around eleven "
     "seconds, at about a quarter of a cent per case. The live traces showed no personal data, and no errors."),
    ("next", slide("Next steps", "What comes after 50/50", """
      <div class='grid' style='grid-template-columns:1fr 1fr 1fr'>
        <div class='card'><h3>Calibrate the judge</h3><p>A person labels 30 replies (Human labels tab); target ≥ 85%
          agreement before relying on judge scores.</p></div>
        <div class='card'><h3>Golden dataset v3</h3><p>v2 is saturated. Build v3 from production failures and
          thumbs-down traces, plus more reschedule and dispute wording.</p></div>
        <div class='card'><h3>Reschedule within policy</h3><p>Today changes go to the office. Next capability: the
          agent reschedules within policy itself.</p></div></div>
      <p class='lead' style='margin-top:30px'>Report: docs/ServiceIQ — Agent Evaluation Report (v2).pdf ·
      Spreadsheet: reports/agent_eval_v2.xlsx · LangSmith dataset serviceiq-agent-golden-v2</p>""", 12),
     "What's next. A person labels thirty replies to calibrate the judge. The dataset is saturated again, so version "
     "three will come from real production failures. And the next capability is letting the agent reschedule within "
     "policy, instead of handing every change to the office. Thank you."),
]


def render(name: str, page: str) -> Path:
    html_path, png = WORK / f"{name}.html", WORK / f"{name}.png"
    html_path.write_text(page, encoding="utf-8")
    subprocess.run([EDGE, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    f"--user-data-dir={WORK / 'edge'}", "--window-size=1280,720", f"--screenshot={png}",
                    html_path.as_uri()], check=False, capture_output=True, timeout=120)
    return png


def speak(client, name: str, text: str) -> Path:
    key = hashlib.sha1(text.encode()).hexdigest()[:10]
    wav = WORK / f"{name}-{key}.wav"
    if not wav.exists():
        audio = client.audio.speech.create(model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="wav",
                                           instructions="A calm, clear presenter walking a team through results.")
        wav.write_bytes(audio.content)
    return wav


def probe(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def main():
    from serviceiq.config import get_settings
    from serviceiq.llm import openai_client
    WORK.mkdir(parents=True, exist_ok=True)
    client = openai_client(get_settings())
    segments, cues, clock = [], [], 0.0
    for name, page, text in SCENES:
        png, wav = render(name, page), speak(client, name, text)
        seg = WORK / f"{name}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(png), "-i", str(wav),
                        "-af", f"atempo={TEMPO},apad=pad_dur={PAD}", "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
                        "-r", "25", "-c:a", "aac", "-ar", "24000", "-shortest", str(seg)], check=True)
        length, spoken = probe(seg), probe(wav) / TEMPO
        sentences = [s.strip() + "." for s in text.replace("? ", "?. ").split(". ") if s.strip()]
        sentences = [s[:-1] if s.endswith("?.") or s.endswith("..") else s for s in sentences]
        total = sum(len(s) for s in sentences)
        t = clock
        for s in sentences:
            d = spoken * len(s) / total
            cues.append((t, t + d, s))
            t += d
        segments.append(seg)
        clock += length
        print(f"{name}: {length:.1f} s")
    (WORK / "list.txt").write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
    (WORK / "subs.srt").write_text("\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{s}\n"
                                             for i, (a, b, s) in enumerate(cues, 1)), encoding="utf-8")
    joined = WORK / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(WORK / "list.txt"),
                    "-c", "copy", str(joined)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-i", str(WORK / "subs.srt"), "-map", "0",
                    "-map", "1", "-c", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
                    str(WORK / "final.mp4")], check=True)
    shutil.copyfile(WORK / "final.mp4", OUT)
    print(f"{OUT.relative_to(ROOT)}: {probe(OUT):.1f} s")


if __name__ == "__main__":
    main()
