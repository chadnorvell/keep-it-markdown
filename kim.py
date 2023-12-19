import argparse
import datetime
import getpass
import imghdr
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from xmlrpc.client import boolean

import click
import gkeepapi
import keyring
import requests
from dotenv import load_dotenv

load_dotenv()

KEEP_KEYRING_ID = "google-keep-token"
KEEP_NOTE_URL = "https://keep.google.com/#NOTE/"

settings = {
    "google_userid": os.environ.get("KIM_GOOGLE_USER_ID", ""),
    "master_token": os.environ.get("KIM_KEEP_MASTER_TOKEN", ""),
    "export_path": os.environ.get("KIM_EXPORT_PATH", "export"),
    "media_path": os.environ.get("KIM_MEDIA_PATH", "media"),
    "fragments_path": os.environ.get("KIM_FRAGMENTS_PATH", "fragments"),
    "import_path": os.environ.get("KIM_IMPORT_PATH", "import"),
    "import_labels": os.environ.get("KIM_IMPORT_LABELS", "my_label"),
}


def download_file(file_url: str, file_name: str, file_path: Path) -> Optional[Path]:
    data_file = file_path / file_name
    r = requests.get(file_url, timeout=10)

    if r.status_code == 200:
        with open(data_file, "wb") as f:
            f.write(r.content)

        return data_file

    return None


def set_file_extension_from_content(data_file: Path) -> Path:
    if imghdr.what(data_file) == "png":
        renamed = data_file.with_suffix(".png")
    elif imghdr.what(data_file) == "jpeg":
        renamed = data_file.with_suffix(".jpg")
    elif imghdr.what(data_file) == "gif":
        renamed = data_file.with_suffix(".gif")
    elif imghdr.what(data_file) == "webp":
        renamed = data_file.with_suffix(".webp")
    else:
        renamed = data_file.with_suffix(".m4a")

    return data_file.rename(renamed)


@dataclass
class Options:
    overwrite: boolean
    archive_only: boolean
    preserve_labels: boolean
    skip_existing: boolean
    text_for_title: boolean
    logseq_style: boolean
    joplin_frontmatter: boolean
    import_files: boolean


@dataclass
class Note:
    id: str
    base_title: str
    text: str
    archived: boolean
    trashed: boolean
    timestamps: dict
    # Labels starting with an uppercase letter are treated as folders, and
    # those starting with a lowercase letter are treated as tags. Don't assign
    # to more than one folder; only one arbitrary option will be used.
    labels: list[str]
    blobs: list
    blob_names: list[str]
    media: list[Path]
    # Essentially datetime.now() when this is run.
    instantiated_when: datetime.datetime = field(default_factory=datetime.datetime.now)

    @property
    def is_empty(self) -> boolean:
        return self.base_title.strip() == "" and self.text.strip() == ""

    @property
    def is_fragment(self) -> boolean:
        return not any(label[0].isupper() for label in self.labels)

    @property
    def created_when(self) -> datetime.datetime:
        if self.timestamps is not None:
            return datetime.datetime.strptime(
                self.timestamps["created"],
                "%Y-%m-%d %H:%M:%S.%f",
            )

        return self.instantiated_when

    @property
    def updated_when(self) -> datetime.datetime:
        if self.timestamps is not None:
            return datetime.datetime.strptime(
                self.timestamps["updated"],
                "%Y-%m-%d %H:%M:%S.%f",
            )

        return self.instantiated_when

    @property
    def media_links(self) -> Iterable[str]:
        for item in self.media:
            yield Markdown.format_path(
                item.relative_to(settings["export_path"]), media=True
            )

    @property
    def title(self) -> str:
        title = "".join(c for c in self.base_title if c.isalnum() or c.isspace())

        # If there's no title or content, try to infer a title from an attachment.
        if title.strip() == "" and self.text.strip() == "":
            try:
                file_type = self.media[0].suffix
            except IndexError:
                file_type = None

            if file_type is not None:
                if file_type in (".jpg", ".png"):
                    title = "Image"

                else:
                    title = "File"

        # If there's no title, try to infer one from the text.
        elif title.strip() == "":
            first_line = self.text.split("\n")[0]
            first_phrase = re.split(r"[\.,:;?!]", first_line)[0]
            first_phrase_clean = "".join(
                c for c in first_phrase if c.isalnum() or c.isspace()
            )
            title = first_phrase_clean.strip()[:64]

        # If it's a fragment, prepend the timestamp. A timestamp-only title is fine.
        if self.is_fragment:
            title_text = title
            title = self.created_when.strftime("%y%m%d%H%M%S")

            if len(title_text) > 0:
                title += f" {title_text}"

        return title

    @property
    def content(self) -> str:
        text = Markdown(self.text).convert_urls().format_check_boxes().text

        if text != "":
            text += "\n\n"

        for media in self.media_links:
            text += f"{media}\n"

        return text

    @property
    def tags(self) -> list[str]:
        return [label for label in self.labels if label[0].islower()]

    @property
    def folder(self) -> str:
        if self.is_fragment:
            return settings["fragments_path"]

        try:
            return [label for label in self.labels if label[0].isupper()][0]
        except IndexError:
            return "."

    @property
    def filename(self) -> str:
        return f"{self.title}.md"

    @property
    def path(self) -> Path:
        return Path(self.folder, self.filename)

    @property
    def front_matter(self) -> str:
        lines = [
            "---",
            f'created: {self.created_when.strftime("%Y-%m-%dT%H:%M")}',
            f'updated: {self.updated_when.strftime("%Y-%m-%dT%H:%M")}',
            f"source: {KEEP_NOTE_URL}{str(self.id)}",
        ]

        if len(self.tags) > 0:
            lines += ["tags:"]

            for tag in self.tags:
                lines += [f"  - {tag}"]

        lines += ["---\n"]
        return "\n".join(lines)

    def populate_media(self, keep) -> None:
        media_path = Path(settings["export_path"], settings["media_path"])

        if not media_path.exists():
            media_path.mkdir(parents=True)

        for idx, blob in enumerate(self.blobs):
            blob_name = f"{self.id}_{str(idx)}"

            if blob is not None:
                url = keep.getmedia(blob)
                blob_file = None
                if url:
                    blob_file = download_file(
                        url,
                        blob_name + ".dat",
                        media_path,
                    )
                    if blob_file:
                        data_file = set_file_extension_from_content(blob_file)
                        self.blob_names.append(blob_name)
                        self.media.append(data_file)
                    else:
                        print("Download of Keep media failed...")

    def save(self) -> None:
        data = self.front_matter + self.content + "\n"
        path = settings["export_path"] / self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8", errors="ignore")

    def conditionally_save(self):
        self.save()


class Markdown:
    def __init__(self, text: str):
        self.text = text

    def convert_urls(self) -> "Markdown":
        # pylint: disable=anomalous-backslash-in-string
        urls = re.findall(
            "http[s]?://(?:[a-zA-Z]|[0-9]|[~#$-_@.&+]"
            "|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
            self.text,
        )
        # Note that the use of temporary %%% is because notes
        #   can have the same URL repeated and replace would fail
        for url in urls:
            self.text = self.text.replace(
                url, f"[{url[:1]}%%%{url[2:]}]({url[:1]}%%%{url[2:]})", 1
            )

        return self.__class__(self.text.replace("h%%%tp", "http"))

    def format_check_boxes(self) -> "Markdown":
        text = self.text.replace("\u2610", "- [ ]").replace("\u2611", "- [x]")
        return self.__class__(text)

    @staticmethod
    def format_path(path: Path, name: Optional[str] = None, media: bool = False):
        sigil = "!" if media else ""
        slug = name if name is not None else path
        return f"{sigil}[{slug}]({path})"


class SecureStorage:
    def __init__(self, userid, keyring_reset, master_token):
        self._userid = userid
        if keyring_reset:
            self._clear_keyring()
        if master_token:
            self.set_keyring(master_token)

    def get_keyring(self):
        self._keep_token = keyring.get_password(KEEP_KEYRING_ID, self._userid)
        return self._keep_token

    def set_keyring(self, keeptoken):
        keyring.set_password(KEEP_KEYRING_ID, self._userid, keeptoken)

    def _clear_keyring(self):
        try:
            keyring.delete_password(KEEP_KEYRING_ID, self._userid)
        except:
            return None
        else:
            return True


class KeepService:
    def __init__(self, userid):
        self._keepapi = gkeepapi.Keep()
        self._userid = userid

    def get_ref(self):
        return self._keepapi

    def keep_sync(self):
        self._keepapi.sync()

    def set_token(self, keyring_reset, master_token):
        if master_token:
            self._keep_token = master_token
        else:
            self._securestorage = SecureStorage(
                self._userid, keyring_reset, master_token
            )
            self._keep_token = self._securestorage.get_keyring()
        return self._keep_token

    def set_user(self, userid):
        self._userid = userid

    def login(self, pw, keyring_reset):
        try:
            self._keepapi.login(self._userid, pw)
        except:
            return None
        else:
            self._keep_token = self._keepapi.getMasterToken()
            if not keyring_reset:
                self._securestorage.set_keyring(self._keep_token)
            return self._keep_token

    def resume(self):
        self._keepapi.resume(self._userid, self._keep_token)

    def getnotes(self):
        return self._keepapi.all()

    def findnotes(self, kquery, labels, archive_only):
        if labels:
            return self._keepapi.find(
                labels=[self._keepapi.findLabel(kquery[1:])],
                archived=archive_only,
                trashed=False,
            )
        else:
            return self._keepapi.find(
                query=kquery, archived=archive_only, trashed=False
            )

    def get_notes(
        self,
        labels: Optional[list[str]] = None,
        pinned: Optional[bool] = None,
        archived: Optional[bool] = None,
        trashed: Optional[bool] = None,
    ):
        if labels is not None:
            label_query = [self._keepapi.findLabel(label) for label in labels]
            label_results = [label for label in label_query if label is not None]

            kwargs: dict[str, Any] = {"labels": label_results}

            if pinned is not None:
                kwargs["pinned"] = pinned

            if archived is not None:
                kwargs["archived"] = archived

            if trashed is not None:
                kwargs["trashed"] = trashed

            return self._keepapi.find(**kwargs)

        return self._keepapi.all()

    def createnote(self, title, notetext):
        self._note = self._keepapi.createNote(title, notetext)
        return None

    def appendnotes(self, kquery, append_text):
        gnotes = self.findnotes(kquery, False, False)
        for gnote in gnotes:
            gnote.text += "\n\n" + append_text
        self.keep_sync()
        return None

    def setnotelabel(self, label):
        try:
            self._labelid = self._keepapi.findLabel(label)
            self._note.labels.add(self._labelid)
        except Exception as e:
            print(
                f"Label doesn't exist! - label: {label} - Use pre-defined labels when importing"
            )
            raise

    def getmedia(self, blob):
        try:
            link = self._keepapi.getMediaLink(blob)
            return link
        except Exception as e:
            return None


def keep_import_notes(keep):
    dir_path = settings["import_path"]
    in_labels = settings["import_labels"].split(",")
    for file in os.listdir(dir_path):
        if os.path.isfile(dir_path + file) and file.endswith(".md"):
            with open(dir_path + file, "r", encoding="utf8") as md_file:
                mod_time = datetime.datetime.fromtimestamp(
                    os.path.getmtime(dir_path + file)
                ).strftime("%Y-%m-%d %H:%M:%S")
                crt_time = datetime.datetime.fromtimestamp(
                    os.path.getctime(dir_path + file)
                ).strftime("%Y-%m-%d %H:%M:%S")
                data = md_file.read()
                data += "\n\nCreated: " + crt_time + "   -   Updated: " + mod_time
                print("Importing note:", file.replace(".md", "") + " from " + file)
                keep.createnote(file.replace(".md", ""), data)
                for in_label in in_labels:
                    keep.setnotelabel(in_label.strip())
                keep.keep_sync()


def keep_query_convert(keep, labels: Optional[list[str]] = None):
    count = 0
    notes = []
    gnotes = keep.get_notes(labels)

    for gnote in gnotes:
        notes.append(
            Note(
                gnote.id,
                gnote.title,
                gnote.text,
                gnote.archived,
                gnote.trashed,
                {
                    "created": str(gnote.timestamps.created),
                    "updated": str(gnote.timestamps.updated),
                },
                [str(label) for label in gnote.labels.all()],
                list(gnote.blobs),
                [],
                [],
            )
        )

    for note in notes:
        note.populate_media(keep)

        if note.title != "" and not note.archived and not note.trashed:
            print(note.path)
            note.conditionally_save()
            count += 1

    return count


# --------------------- UI / CLI ------------------------------


def ui_login(keyring_reset, master_token):
    try:
        userid = settings["google_userid"].strip().lower()

        if userid == "":
            userid = click.prompt("Enter your Google account username", type=str)
        else:
            print(f"Your Google account name is: {userid} -- Welcome!")

        # 0.5.0 work
        keep = KeepService(userid)
        ktoken = keep.set_token(keyring_reset, master_token)

        if ktoken is None:
            pw = getpass.getpass(prompt="Enter your Google Password: ", stream=None)
            print("\r\n\r\nOne moment...")

            ktoken = keep.login(pw, keyring_reset)
            if ktoken:
                if keyring_reset:
                    print("You've succesfully logged into Google Keep!")
                else:
                    print(
                        "You've succesfully logged into Google Keep! "
                        "Your Keep access token has been securely stored "
                        "in this computer's keyring."
                    )
            # else:
            #  print ("Invalid Google userid or pw! Please try again.")

        else:
            print(
                "You've succesfully logged into Google Keep using local keyring access token!"
            )

        keep.resume()
        return keep

    except Exception as e:
        print("\r\nUsername or password is incorrect (" + repr(e) + ")")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--labels", type=str, nargs="+")
    args = parser.parse_args()

    keep = ui_login(False, settings["master_token"])
    count = keep_query_convert(keep, labels=args.labels)
    print("\nTotal converted notes: " + str(count))


# Version 0.5.2

if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
