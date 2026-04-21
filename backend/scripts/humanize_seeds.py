#!/usr/bin/env python3
"""
Programmatically injects human speech patterns into seed conversation transcripts.
Modifies the text of each segment to sound more natural and less robotic.
"""

import re
import random
import importlib
import importlib.util
import sys
import os

# Filler words and patterns to inject
FILLERS = ["um, ", "uh, ", "like, ", "you know, ", "so, ", "I mean, ", "basically, "]
STARTERS = [
    "So, ", "Yeah, so, ", "Okay, so, ", "Right, so, ", "Um, ", "Well, ",
    "I mean, ", "Honestly, ", "Look, ", "Alright, so, ",
]
ACKNOWLEDGMENTS = [
    "Mhm. ", "Right, right. ", "Gotcha. ", "Okay, okay. ", "Yeah. ",
    "Sure, sure. ", "Ah, I see. ", "Makes sense. ", "Got it. ",
]
INTERJECTIONS = [
    " — sorry, what was I saying — ",
    " — actually, hold on — ",
    " — wait, let me think about that — ",
    " — oh, and one more thing — ",
]
CORRECTIONS = [
    (" about ", [" about — well, roughly "]),
    (" three ", [" three — no wait, "]),
    (" our ", [" our — well, my team's "]),
]
TRAILING = [
    "... if that makes sense?",
    "... does that make sense?",
    "... you know what I mean?",
    "... right?",
    "... so yeah.",
]
HEDGES = [
    "I think ", "I believe ", "I'm pretty sure ", "From what I remember, ",
    "If I'm not mistaken, ", "As far as I know, ",
]
VERBAL_TICS = [
    (".", ". [pause] "),
    (". ", ". So. "),
]
LAUGHTER = [" [laughs] ", " [chuckles] "]
BACKGROUND = [
    " — sorry, someone just walked in — ",
    " — hold on one sec — okay, I'm back — ",
    " — sorry, can you hear me okay? — ",
]


def humanize_text(text, speaker_id, segment_index, total_segments):
    """Add human speech patterns to a single segment's text."""
    original = text

    # Don't modify very short texts
    if len(text) < 30:
        return text

    modifications = 0
    max_mods = random.randint(1, 3)  # 1-3 modifications per segment

    # 1. Sometimes add a filler starter (30% chance)
    if random.random() < 0.30 and modifications < max_mods:
        if speaker_id == "customer":
            text = random.choice(STARTERS) + text[0].lower() + text[1:]
        else:
            text = random.choice(STARTERS[:6]) + text[0].lower() + text[1:]
        modifications += 1

    # 2. Sometimes add acknowledgment at start for non-first segments (25% for agents)
    if segment_index > 0 and speaker_id == "agent" and random.random() < 0.25 and modifications < max_mods:
        text = random.choice(ACKNOWLEDGMENTS) + text
        modifications += 1

    # 3. Insert a filler word mid-sentence (20% chance)
    if random.random() < 0.20 and modifications < max_mods:
        sentences = text.split(". ")
        if len(sentences) > 1:
            idx = random.randint(0, len(sentences) - 1)
            words = sentences[idx].split()
            if len(words) > 5:
                insert_pos = random.randint(2, len(words) - 2)
                words.insert(insert_pos, random.choice(FILLERS).strip().rstrip(","))
                sentences[idx] = " ".join(words)
                text = ". ".join(sentences)
                modifications += 1

    # 4. Add trailing phrase at end (15% chance)
    if random.random() < 0.15 and modifications < max_mods:
        if text.endswith("."):
            text = text[:-1] + random.choice(TRAILING)
        modifications += 1

    # 5. Add hedge before a statement (10% chance)
    if random.random() < 0.10 and modifications < max_mods:
        sentences = text.split(". ")
        if len(sentences) > 1:
            idx = random.randint(1, len(sentences) - 1)
            sentences[idx] = random.choice(HEDGES) + sentences[idx][0].lower() + sentences[idx][1:]
            text = ". ".join(sentences)
            modifications += 1

    # 6. Add correction mid-sentence (8% chance)
    if random.random() < 0.08 and modifications < max_mods:
        for pattern, replacements in CORRECTIONS:
            if pattern in text:
                text = text.replace(pattern, random.choice(replacements), 1)
                modifications += 1
                break

    # 7. Add laughter (5% chance, more for positive sentiment)
    if random.random() < 0.05 and modifications < max_mods:
        sentences = text.split(". ")
        if len(sentences) > 1:
            idx = random.randint(0, len(sentences) - 1)
            sentences[idx] += random.choice(LAUGHTER)
            text = ". ".join(sentences)
            modifications += 1

    # 8. Contract formal phrases
    contractions = {
        "I am ": "I'm ", "I have ": "I've ", "I will ": "I'll ",
        "I would ": "I'd ", "we are ": "we're ", "we have ": "we've ",
        "we will ": "we'll ", "they are ": "they're ", "that is ": "that's ",
        "it is ": "it's ", "cannot ": "can't ", "do not ": "don't ",
        "does not ": "doesn't ", "did not ": "didn't ", "would not ": "wouldn't ",
        "should not ": "shouldn't ", "could not ": "couldn't ",
        "is not ": "isn't ", "are not ": "aren't ", "was not ": "wasn't ",
        "going to ": "gonna ", "want to ": "wanna ", "kind of ": "kinda ",
        "got to ": "gotta ",
    }
    for formal, informal in contractions.items():
        if random.random() < 0.6:  # 60% chance to contract each instance
            text = text.replace(formal, informal)

    # 9. Add background noise (2% chance)
    if random.random() < 0.02:
        words = text.split()
        if len(words) > 8:
            pos = random.randint(3, len(words) - 3)
            words.insert(pos, random.choice(BACKGROUND))
            text = " ".join(words)

    return text


def humanize_file(filepath, list_name):
    """Humanize all conversations in a seed file."""
    # Import the module
    module_name = os.path.basename(filepath).replace(".py", "")
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls = getattr(module, list_name)
    print(f"\n{'='*60}")
    print(f"Humanizing {filepath} ({len(calls)} conversations)")
    print(f"{'='*60}")

    random.seed(42)  # Reproducible

    # Read the file
    with open(filepath, "r") as f:
        content = f.read()

    total_changes = 0
    for call_idx, call in enumerate(calls):
        segments = call.get("segments", [])
        for seg_idx, seg in enumerate(segments):
            original_text = seg["text"]
            new_text = humanize_text(
                original_text,
                seg["speaker_id"],
                seg_idx,
                len(segments),
            )
            if new_text != original_text:
                # Escape for string replacement
                # Use the exact original text to find and replace in file
                old_repr = repr(original_text)
                new_repr = repr(new_text)
                if old_repr in content:
                    content = content.replace(old_repr, new_repr, 1)
                    total_changes += 1

    # Write back
    with open(filepath, "w") as f:
        f.write(content)

    print(f"Modified {total_changes} segments")
    return total_changes


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    total = 0
    total += humanize_file("seed_sales.py", "SALES_CALLS")
    total += humanize_file("seed_it.py", "IT_CALLS")
    total += humanize_file("seed_cs.py", "CS_CALLS")

    print(f"\n{'='*60}")
    print(f"Total segments humanized: {total}")
    print(f"{'='*60}")
    print("\nDone! Run 'python3 backend/seed.py' to re-seed the database.")
