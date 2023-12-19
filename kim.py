import configparser
import datetime
import getpass
import imghdr
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from xmlrpc.client import boolean

import click
import gkeepapi
import keyring
import requests

KEEP_KEYRING_ID = "google-keep-token"
KEEP_NOTE_URL = "https://keep.google.com/#NOTE/"
CONFIG_FILE = "settings.cfg"
DEFAULT_SECTION = "SETTINGS"
USERID_EMPTY = "add your google account name here"
OUTPUTPATH = "mdfiles"
MEDIADEFAULTPATH = "media"
INPUTDEFAULTPATH = "import/markdown_files"
DEFAULT_LABELS = "my_label"
DEFAULT_SEPARATOR = "/"
MAX_FILENAME_LENGTH = 99
MISSING = "null value"

TECH_ERR = " Technical Error Message: "

CONFIG_FILE_MESSAGE = (
    f"Your {CONFIG_FILE} file contains to the following "
    f"[{DEFAULT_SECTION}] values. Be sure to edit it with "
    " your information."
)
MALFORMED_CONFIG_FILE = (
    f"The {CONFIG_FILE} default settings file exists but "
    f"has a malformed header - header should be [{DEFAULT_SECTION}]"
)
UNKNOWNN_CONFIG_FILE = (
    "There is an unknown configuration file issue - "
    f"{CONFIG_FILE} or file system may be locked or "
    "corrupted. Try deleting the file and recreating it."
)
MISSING_CONFIG_FILE = (
    f"The configuration file - {CONFIG_FILE} is missing. "
    "Please check the documention on recreating it"
)
BADFILE_CONFIG_FILE = (
    f"Unable to create {CONFIG_FILE}. "
    "The file system issue such as locked or corrupted"
)
KEYERR_CONFIG_FILE = f"Configuration key in {CONFIG_FILE} not found. Key passed is: "


ILLEGAL_FILE_CHARS = [
    "<",
    ">",
    ":",
    '"',
    "/",
    "\\",
    "|",
    "?",
    "*",
    "&",
    "\n",
    "\r",
    "\t",
]
ILLEGAL_TAG_CHARS = [
    "~",
    "`",
    "!",
    "@",
    "$",
    "%",
    "^",
    "(",
    ")",
    "+",
    "=",
    "{",
    "}",
    "[",
    "]",
    "<",
    ">",
    ";",
    ":",
    ",",
    ".",
    '"',
    "/",
    "\\",
    "|",
    "?",
    "*",
    "&",
    "\n",
    "\r",
]

default_settings = {
    "google_userid": USERID_EMPTY,
    "output_path": OUTPUTPATH,
    "media_path": MEDIADEFAULTPATH,
    "input_path": INPUTDEFAULTPATH,
    "input_labels": DEFAULT_LABELS,
    "folder_separator": DEFAULT_SEPARATOR,
}


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
    labels: list
    blobs: list
    blob_names: list
    media: list
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
    def title(self) -> str:
        title = "".join(c for c in self.base_title if c.isalnum() or c.isspace())

        # If there's no title or content, try to infer a title from an attachment.
        if title.strip() == "" and self.text.strip() == "":
            try:
                file_type = self.media[0].split(".")[-1]
            except IndexError:
                file_type = None

            if file_type is not None:
                if file_type in ("jpg", "png"):
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

        for media in self.media:
            text += (
                Markdown.format_path(
                    Config().get("media_path") + "/" + media, "", True, "_"
                )
                + "\n"
            )

        return text

    @property
    def tags(self) -> list[str]:
        return [label for label in self.labels if label[0].islower()]

    @property
    def folder(self) -> str:
        if self.is_fragment:
            return "Fragments"

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

    def populate_media(self, keep):
        fs = FileService()

        for idx, blob in enumerate(self.blobs):
            blob_name = f"{self.id}_{str(idx)}"

            if blob is not None:
                url = keep.getmedia(blob)
                blob_file = None
                if url:
                    blob_file = fs.download_file(
                        url, blob_name + ".dat", fs.media_path()
                    )
                    if blob_file:
                        data_file = fs.set_file_extensions(
                            blob_file, blob_name, fs.media_path()
                        )
                        self.blob_names.append(blob_name)
                        self.media.append(data_file)
                    else:
                        print("Download of Keep media failed...")

    def save(self):
        fs = FileService()

        md_text = self.content

        for media in self.media:
            md_text = (
                Markdown.format_path(
                    Config().get("media_path") + "/" + media, "", True, "_"
                )
                + "\n"
                + md_text
            )

        md_file = Path(fs.outpath(), self.path)

        markdown_data = self.front_matter + self.content + "\n"

        fs.write_file(md_file, markdown_data)

    def conditionally_save(self):
        self.save()


class ConfigurationException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


# This is a singleton class instance - not really necessary but saves a tiny bit of memory
# Very useful for single connections and loading config files once
class Config:
    _config = configparser.ConfigParser()
    _configdict = {}

    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = super(Config, cls).__new__(cls)
            cls.instance.__read()
            cls.instance.__load()
        return cls.instance

    def __read(self):
        try:
            self._cfile = self._config.read(CONFIG_FILE)
            if not self._cfile:
                self.__create()
        except configparser.MissingSectionHeaderError:
            raise ConfigurationException(MALFORMED_CONFIG_FILE)
        except Exception:
            raise ConfigurationException(UNKNOWNN_CONFIG_FILE)

    def __create(self):
        self._config[DEFAULT_SECTION] = default_settings
        try:
            with open(CONFIG_FILE, "w") as configfile:
                self._config.write(configfile)
        except Exception as e:
            raise ConfigurationException(BADFILE_CONFIG_FILE)

    def __load(self):
        options = self._config.options(DEFAULT_SECTION)
        for option in options:
            self._configdict[option] = self._config.get(DEFAULT_SECTION, option)

    def get(self, key):
        try:
            return self._configdict[key]
        except Exception as e:
            raise ConfigurationException(KEYERR_CONFIG_FILE + key)


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
        text = self.text.replace("\u2610", "- [ ]").replace("\u2611", " - [x]")
        return self.__class__(text)

    @staticmethod
    def format_path(path, name, media, replacement):
        if media:
            header = "!["
        else:
            header = "["
        path = path.replace(" ", replacement)
        if name:
            return header + name + "](" + path + ")"
        else:
            return header + path + "](" + path + ")"


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
        self._securestorage = SecureStorage(self._userid, keyring_reset, master_token)
        if master_token:
            self._keep_token = master_token
        else:
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


class NameService:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = super(NameService, cls).__new__(cls)
            cls.instance._namelist = []
        return cls.instance

    def clear_name_list(self):
        self._namelist.clear()

    def check_duplicate_name(self, note_title, note_date):
        if note_title in self._namelist:
            note_title = note_title + note_date
            note_title = self.check_duplicate_name(note_title, note_date)
        self._namelist.append(note_title)
        return note_title

    def check_file_exists(self, md_file, outpath, note_title, note_date):
        # md_file = Path(outpath, note_title + ".md")
        self._namelist.remove(note_title)
        while md_file.exists():
            note_title = self.check_duplicate_name(note_title, note_date)
            self._namelist.append(note_title)
            md_file = Path(outpath, note_title + ".md")
        return note_title


class FileService:
    def media_path(self):
        outpath = Config().get("output_path").rstrip("/")
        mediapath = outpath + "/" + Config().get("media_path").rstrip("/") + "/"
        return mediapath

    def outpath(self):
        outpath = Config().get("output_path").rstrip("/")
        return outpath

    def inpath(self):
        inpath = Config().get("input_path").rstrip("/") + "/"
        return inpath

    def create_path(self, path):
        if not os.path.exists(path):
            os.mkdir(path)

    def write_file(self, file_name, data):
        if file_name.parent != Path("."):
            file_name.parent.mkdir(parents=True, exist_ok=True)

        try:
            f = open(file_name, "w+", encoding="utf-8", errors="ignore")
            f.write(data)
            f.close()
        except Exception as e:
            raise Exception("Error in write_file: " + " -- " + TECH_ERR + repr(e))

    def download_file(self, file_url, file_name, file_path):
        try:
            data_file = file_path + file_name
            r = requests.get(file_url)
            if r.status_code == 200:
                with open(data_file, "wb") as f:
                    f.write(r.content)
                    f.close
                return data_file

            else:
                blob_final_path = "Media could not be retrieved"
                return ""

        except:
            print("Error in download_file()")
            raise

    def set_file_extensions(self, data_file, file_name, file_path):
        dest_path = file_path + file_name

        if imghdr.what(data_file) == "png":
            media_name = file_name + ".png"
            blob_final_path = dest_path + ".png"
        elif imghdr.what(data_file) == "jpeg":
            media_name = file_name + ".jpg"
            blob_final_path = dest_path + ".jpg"
        elif imghdr.what(data_file) == "gif":
            media_name = file_name + ".gif"
            blob_final_path = dest_path + ".gif"
        elif imghdr.what(data_file) == "webp":
            media_name = file_name + ".webp"
            blob_final_path = dest_path + ".webp"
        else:
            extension = ".m4a"
            media_name = file_name + extension
            blob_final_path = dest_path + extension

        shutil.copyfile(data_file, blob_final_path)

        if os.path.exists(data_file):
            os.remove(data_file)

        return media_name


def keep_import_notes(keep):
    try:
        dir_path = FileService().inpath()
        in_labels = Config().get("input_labels").split(",")
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
    except Exception as e:
        print("Error on note import:", str(e))


def keep_query_convert(keep, keepquery, opts):
    try:
        count = 0

        if keepquery == "--all":
            gnotes = keep.getnotes()
        else:
            if keepquery[0] == "#":
                gnotes = keep.findnotes(keepquery, True, opts.archive_only)
            else:
                gnotes = keep.findnotes(keepquery, False, opts.archive_only)

        notes = []

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

    except:
        print("Error in keep_query_convert()")
        raise


# --------------------- UI / CLI ------------------------------


def ui_login(keyring_reset, master_token):
    try:
        userid = Config().get("google_userid").strip().lower()

        if userid == USERID_EMPTY:
            userid = click.prompt("Enter your Google account username", type=str)
        else:
            print(
                f"Your Google account name is: {userid} -- Welcome!"
            )

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


def ui_query(keep, search_term, opts):
    try:
        if search_term is not None:
            count = keep_query_convert(keep, search_term, opts)
            print("\nTotal converted notes: " + str(count))
            return
        else:
            kquery = "kquery"
            while kquery:
                kquery = click.prompt(
                    "\r\nEnter a keyword search, label search or "
                    + "'--all' to convert Keep notes to md or '--x' to exit",
                    type=str,
                )
                if kquery != "--x":
                    count = keep_query_convert(keep, kquery, opts)
                    print("\nTotal converted notes: " + str(count))
                else:
                    return
    except Exception as e:
        print("Conversion to markdown error - " + repr(e) + " ")
        raise


def ui_welcome_config():
    try:
        mp = Config().get("media_path")

        if (":" in mp) or (mp[0] == "/"):
            raise ValueError(
                f"Media path: '{mp}' within your config file - {CONFIG_FILE}"
                " - must be relative to the output path and cannot start with / or a drive-mount"
            )

        # Make sure paths are set before doing anything
        fs = FileService()
        fs.create_path(fs.outpath())
        fs.create_path(fs.media_path())

        # return defaults
    except Exception as e:
        print(f"\r\nConfiguration file error - {CONFIG_FILE} - {repr(e)}")
        raise


@click.command()
@click.option(
    "-r",
    is_flag=True,
    help="Will reset and not use the local keep access token in your system's keyring",
)
@click.option(
    "-o", is_flag=True, help="Overwrite any existing markdown files with the same name"
)
@click.option("-a", is_flag=True, help="Search and export only archived notes")
@click.option(
    "-p", is_flag=True, help="Preserve keep labels with spaces and special characters"
)
@click.option(
    "-s", is_flag=True, help="Skip over any existing notes with the same title"
)
@click.option(
    "-c",
    is_flag=True,
    help="Use starting content within note body instead of create date for md filename",
)
@click.option("-l", is_flag=True, help="Prepend paragraphs with Logseq style bullets")
@click.option(
    "-j", is_flag=True, help="Prepend notes with Joplin front matter tags and dates"
)
@click.option(
    "-i", is_flag=True, help="Import notes from markdown files EXPERIMENTAL!!"
)
@click.option(
    "-b", "--search-term", help="Run in batch mode with a specific Keep search term"
)
@click.option("-t", "--master-token", help="Log in using master keep token")
def main(r, o, a, p, s, c, l, j, i, search_term, master_token):
    try:
        # j = True
        opts = Options(o, a, p, s, c, l, j, i)
        click.echo("\r\nWelcome to Keep it Markdown or KIM!\r\n")

        if i and (r or o or a or s or p or c):
            print(
                "Importing markdown notes with export options is not compatible -- "
                "please use -i only to import"
            )
            exit()

        if o and s:
            print(
                "Overwrite and Skip flags are not compatible together -- "
                "please use one or the other..."
            )
            exit()

        ui_welcome_config()

        keep = ui_login(r, master_token)

        if i:
            keep_import_notes(keep)
        else:
            ui_query(keep, search_term, opts)

    except:
        print("Could not excute KIM")
    # except Exception as e:
    #    raise Exception("Problem with markdown file creation: " + repr(e))


# Version 0.5.2

if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
