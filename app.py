"""
Proof of Grass: one photo a day, taken outside, judged by an open-weight AI.

Every day your friend group gets the same prompt. You go outside, take the photo,
and OpenAI's open-weight CLIP model (running on your own machine) decides whether
it counts. Pass and you earn points, keep your streak, and unlock your friends'
photos, which stay blurred until you post.

Run locally:            pip install -r requirements.txt  then  python app.py
Share with your phone:  python app.py --share
"""

import os
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gradio as gr
from PIL import Image, ImageFilter, ImageOps
from transformers import pipeline

# ---------------------------------------------------------------- settings
# Set TIMEZONE so "today" means the same thing for everyone, e.g. "Africa/Nairobi".
# Leave it unset to use this computer's local time.
TIMEZONE = os.environ.get("TIMEZONE")

OUTDOOR_THRESHOLD = 0.5  # raise to make the "is it outside?" check stricter
MATCH_THRESHOLD = 0.3    # raise to make the "does it match the prompt?" check stricter

DATA = Path(os.environ.get("DATA_DIR", "data"))
PHOTOS = DATA / "photos"
PHOTOS.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "grass.db"

# What players see, and the sentence CLIP compares the photo against.
PROMPTS = [
    ("Something orange", "a photo of something orange outdoors"),
    ("A tree taller than a building", "a photo of a very tall tree"),
    ("Your shoes on grass or dirt", "a photo of shoes standing on grass or dirt"),
    ("A single fallen leaf", "a close-up photo of a fallen leaf on the ground"),
    ("The sky right now", "a photo of the sky and clouds"),
    ("Water in the wild", "a photo of a lake, river, pond or puddle"),
    ("A flower", "a close-up photo of a flower"),
    ("Moss or mushrooms", "a close-up photo of moss or mushrooms"),
    ("A path you have never walked", "a photo of a path or hiking trail"),
    ("A park bench", "a photo of a bench in a park"),
    ("Interesting rocks", "a close-up photo of rocks and stones"),
    ("A pinecone, acorn or seed", "a close-up photo of a pinecone, acorn or seed"),
    ("Your shadow", "a photo of a person's shadow on the ground"),
    ("Tree bark up close", "a close-up photo of tree bark"),
]

OUTDOOR = [
    "a photo taken outdoors in nature",
    "a photo taken outside on a street or in a park",
]
INDOOR = [
    "a photo taken indoors in a room",
    "a photo of a computer, TV or phone screen",
    "a screenshot of an app or website",
]


def now():
    return datetime.now(ZoneInfo(TIMEZONE)) if TIMEZONE else datetime.now()


def today():
    return now().date()


def prompt_for(day):
    """Same prompt for everyone on a given day."""
    return PROMPTS[day.toordinal() % len(PROMPTS)]


def decoys_for(day, prompt, n=5):
    """Same decoys for everyone on a given day, so the judge is equally fair to all."""
    others = [p for p in PROMPTS if p != prompt]
    random.Random(day.toordinal()).shuffle(others)
    return others[:n]


# ---------------------------------------------------------------- the judge
print("Loading CLIP (downloads about 600 MB the first time)...")
clip = pipeline("zero-shot-image-classification", model="openai/clip-vit-base-patch32")


def clip_scores(image, labels):
    """CLIP compares the image with each sentence. Scores add up to 1 (softmax)."""
    results = clip(image, candidate_labels=labels, hypothesis_template="{}")
    return {r["label"]: r["score"] for r in results}


def judge(image, day):
    text, label = prompt_for(day)
    decoys = decoys_for(day, (text, label))

    # Check 1: is it actually outside (and not a photo of a screen)?
    scene = clip_scores(image, OUTDOOR + INDOOR)
    outdoor_score = sum(scene[l] for l in OUTDOOR)

    # Check 2: does it match today's prompt better than today's decoys?
    match = clip_scores(image, [label] + [d_label for _, d_label in decoys])
    match_score = match[label]
    best = max(match, key=match.get)
    looked_like = next((d_text for d_text, d_label in decoys if d_label == best), None)

    outdoors = outdoor_score >= OUTDOOR_THRESHOLD
    matches = best == label or match_score >= MATCH_THRESHOLD
    return {
        "passed": outdoors and matches,
        "outdoors": outdoors,
        "outdoor_score": outdoor_score,
        "match_score": match_score,
        "looked_like": looked_like,
    }


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


with db() as con:
    con.execute("""
        create table if not exists posts (
            id text primary key,
            group_code text not null,
            player text not null,
            day text not null,
            prompt text not null,
            photo text not null,
            outdoor_score real,
            match_score real,
            points integer not null,
            created_at text not null,
            unique (group_code, player, day)
        )
    """)


def posts_since(group, since_day):
    with db() as con:
        return con.execute(
            "select * from posts where group_code = ? and day >= ? order by created_at",
            (group, since_day.isoformat()),
        ).fetchall()


def streak(posts, player):
    days = {p["day"] for p in posts if p["player"] == player}
    cursor = today() if today().isoformat() in days else today() - timedelta(days=1)
    n = 0
    while cursor.isoformat() in days:
        n += 1
        cursor -= timedelta(days=1)
    return n


# ---------------------------------------------------------------- photo checks
def taken_before_today(img):
    """Best-effort anti-cheat: reject camera-roll photos whose EXIF date isn't today.
    Returns False when there's no EXIF date (e.g. webcam captures), so they're allowed."""
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal or DateTime
        if not raw:
            return False
        taken = datetime.strptime(raw.strip("\x00 "), "%Y:%m:%d %H:%M:%S").date()
        return taken < today()
    except Exception:
        return False


# ---------------------------------------------------------------- screens
def clean(s):
    return (s or "").strip()[:24]


def view(player, group):
    """Everything on the page except the result message."""
    player, group = clean(player), clean(group).lower()
    day = today()
    text, _ = prompt_for(day)
    prompt_md = f"### Today's prompt: **{text}**"

    if not player or not group:
        return prompt_md, [], [], "Enter your name and group code, then press **Refresh**."

    posts = posts_since(group, day - timedelta(days=30))
    todays = [p for p in posts if p["day"] == day.isoformat()]
    posted = any(p["player"] == player for p in todays)

    # Feed: friends' photos stay blurred until you've posted yours.
    gallery = []
    for p in todays:
        img = Image.open(p["photo"])
        if not posted and p["player"] != player:
            img = img.filter(ImageFilter.GaussianBlur(24))
            caption = f"🔒 {p['player']} (post to unlock)"
        else:
            caption = f"{p['player']} +{p['points']}"
        gallery.append((img, caption))

    # Leaderboard: points from the last 7 days, plus current streak.
    week_start = (day - timedelta(days=6)).isoformat()
    totals = {}
    for p in posts:
        if p["day"] >= week_start:
            totals[p["player"]] = totals.get(p["player"], 0) + p["points"]
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], -streak(posts, kv[0])))
    board = [[i + 1, name, streak(posts, name), pts] for i, (name, pts) in enumerate(ranked)]

    if posted:
        status = f"✅ You touched grass today. Streak: **{streak(posts, player)}** 🔥"
    elif todays:
        status = "Your friends are out there. Their photos unlock when you post."
    else:
        status = "Nobody has been out yet. First one gets **+5**."
    return prompt_md, gallery, board, status


def submit(player, group, photo_path):
    player, group = clean(player), clean(group).lower()

    def done(message):
        return (message, *view(player, group))

    if not player or not group:
        return done("Enter your name and a group code first.")
    if not photo_path:
        return done("Take a photo first.")

    day = today()
    if any(p["player"] == player for p in posts_since(group, day)):
        return done("You already posted today. Come back tomorrow!")

    img = Image.open(photo_path)
    if taken_before_today(img):
        return done("That photo is from your camera roll, not today. Fresh air only: take a new one.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((640, 640))

    r = judge(img, day)
    scores = f"_outdoors {r['outdoor_score']:.0%} · matches prompt {r['match_score']:.0%}_"

    if not r["passed"]:
        if not r["outdoors"]:
            why = "That looks like it was taken indoors. Go outside and try again."
        else:
            why = f"That doesn't look like **{prompt_for(day)[0]}**"
            if r["looked_like"]:
                why += f" (it looked more like *{r['looked_like']}*)"
            why += ". Try again."
        return done(f"**Not quite.** {why}\n\n{scores}")

    first = not posts_since(group, day)
    points = 10 + round(r["match_score"] * 10) + (5 if first else 0)
    path = PHOTOS / f"{uuid.uuid4()}.jpg"
    img.save(path, quality=80)

    with db() as con:
        con.execute(
            "insert into posts values (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), group, player, day.isoformat(), prompt_for(day)[0], str(path),
             r["outdoor_score"], r["match_score"], points, now().isoformat()),
        )

    bonus = " (first out today!)" if first else ""
    return done(f"**🌱 Grass touched.** +{points} pts{bonus}\n\n{scores}")


# ---------------------------------------------------------------- page
with gr.Blocks(title="Proof of Grass") as demo:
    gr.Markdown("# 🌱 Proof of Grass\nOne photo a day. Taken outside. Judged by an open-weight AI.")
    with gr.Row():
        player = gr.Textbox(label="Your name", max_lines=1)
        group = gr.Textbox(label="Friend group code", max_lines=1, placeholder="e.g. trailgoblins")
    prompt_md = gr.Markdown()
    status = gr.Markdown()
    photo = gr.Image(label="Go outside and snap it", type="filepath", sources=["upload", "webcam"])
    submit_btn = gr.Button("Submit to the grass judge", variant="primary")
    result = gr.Markdown()
    gallery = gr.Gallery(label="Today in your group", columns=2, height="auto")
    board = gr.Dataframe(
        headers=["#", "Player", "Streak 🔥", "Points this week"],
        label="This week",
        interactive=False,
    )
    refresh_btn = gr.Button("Refresh")

    page = [prompt_md, gallery, board, status]
    demo.load(view, [player, group], page)
    refresh_btn.click(view, [player, group], page)
    group.submit(view, [player, group], page)
    submit_btn.click(submit, [player, group, photo], [result, *page])


if __name__ == "__main__":
    demo.launch(share="--share" in sys.argv)
