import binascii
import io
import zipfile
from datetime import datetime, timezone
from typing import Any

import docutils.core
import tornado.web

from pinnwand import (
    defensive,
    error,
    logger,
    path,
    utility,
)
from pinnwand.configuration import Configuration, ConfigurationProvider
from pinnwand.database import manager, models

log = logger.get_logger(__name__)


class Base(tornado.web.RequestHandler):
    """Base page for all 'web' pages to inherit from. This page handles
    default methods for GET and POST but more importantly overwrites
    `write_error` to render error pages.

    It automatically converts ValidationError to a 400 error page but leaves
    other HTTPErrors alone."""

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        if status_code == 404:
            self.render(
                "error.html",
                text="That page does not exist",
                status_code=404,
                pagetitle="error",
            )
        else:
            type_, exc, traceback = kwargs["exc_info"]

            if type_ == error.ValidationError:
                self.set_status(400)
                self.render(
                    "error.html",
                    text=str(exc),
                    status_code=400,
                    pagetitle="error",
                )
            elif type_ == error.RatelimitError:
                self.set_status(429)
                self.render(
                    "error.html",
                    text=str(exc),
                    status_code=429,
                    pagetitle="error",
                )
            elif type_ == error.SpamError:
                self.set_status(451)
                self.render(
                    "error.html",
                    text=str(exc),
                    status_code=429,
                    pagetitle="error",
                )
            else:
                self.render(
                    "error.html",
                    text="unknown error",
                    status_code=500,
                    pagetitle="error",
                )

    async def get(self) -> None:
        raise tornado.web.HTTPError(404)

    async def post(self) -> None:
        raise tornado.web.HTTPError(405)


class Create(Base):
    """The index page shows the new paste page with a list of all available
    lexers from Pygments."""

    @defensive.ratelimit(area="read")
    async def get(self, lexers: str = "") -> None:
        """Render the new paste form, optionally have a lexer preselected from
        the URL."""

        configuration: Configuration = ConfigurationProvider.get_config()

        lexers_available = utility.list_languages()
        lexers_selected = [
            lexer for lexer in lexers.split("+") if lexer.strip()
        ]

        if not lexers_selected:
            lexers_selected = [configuration.default_selected_lexer]

        # Make sure all lexers are available
        if not all(lexer in lexers_available for lexer in lexers_selected):
            log.debug("CreatePaste.get: non-existent lexer requested")
            raise tornado.web.HTTPError(404)

        await self.render(
            "create.html",
            expiries=configuration.expiries,
            lexers=lexers_selected,
            lexers_available=lexers_available,
            pagetitle="Create new paste",
            message=None,
            paste=None,
        )

    @defensive.ratelimit(area="create")
    async def post(self) -> None:
        """This is a historical endpoint to create pastes, pastes are marked as
        old-web and will get a warning on top of them to remove any access to
        this route.

        pinnwand has since evolved with an API which should be used and a
        multi-file paste.

        See the 'CreateAction' for the new-style creation of pastes."""

        lexer = self.get_body_argument("lexer")
        raw = self.get_body_argument("code", strip=False)
        expiry = self.get_body_argument("expiry")
        configuration: Configuration = ConfigurationProvider.get_config()

        if lexer not in utility.list_languages():
            log.info("Paste.post: a paste was submitted with an invalid lexer")
            raise tornado.web.HTTPError(400)

        # Guard against empty strings
        if not raw or not raw.strip():
            return self.redirect(f"/+{lexer}")

        if expiry not in configuration.expiries:
            log.info("Paste.post: a paste was submitted with an invalid expiry")
            raise tornado.web.HTTPError(400)

        paste = models.Paste(
            utility.slug_create(),
            configuration.expiries[expiry],
            "deprecated-web",
        )
        file = models.File(paste.slug, raw, lexer)
        paste.files.append(file)

        with manager.DatabaseManager.get_session() as session:
            session.add(paste)
            session.commit()

            # The removal cookie is set for the specific path of the paste it is
            # related to
            self.set_cookie(
                "removal", str(paste.removal), path=f"/{paste.slug}"
            )

            # Send the client to the paste
            self.redirect(f"/{paste.slug}")

    def check_xsrf_cookie(self) -> None:
        """The CSRF token check is disabled. While it would be better if it
        was on the impact is both small (someone could make a paste in
        a users name which could allow pinnwand to be used as a vector for
        exfiltration from other XSS) and some command line utilities
        POST directly to this endpoint without using the JSON endpoint."""
        return


class CreateAction(Base):
    """The create action is the 'new' way to create pastes and supports multi
    file pastes."""

    @defensive.ratelimit(area="create")
    def post(self) -> None:  # type: ignore
        """POST handler for the 'web' side of things."""

        configuration: Configuration = ConfigurationProvider.get_config()
        expiry = self.get_body_argument("expiry")

        if expiry not in configuration.expiries:
            log.info(
                "CreateAction.post: a paste was submitted with an invalid expiry"
            )
            raise error.ValidationError("Invalid expiry provided")

        auto_scale = self.get_body_argument("long", None) is None

        lexers = self.get_body_arguments("lexer")
        raws = self.get_body_arguments("raw", strip=False)
        filenames = self.get_body_arguments("filename")

        if not all([lexers, raws, filenames]):
            # Prevent empty argument lists from making it through
            raise error.ValidationError(
                "'lexers', 'raws', and 'filenames' arguments must not be empty"
            )

        if not all(raw.strip() for raw in raws):
            # Prevent empty raws from making it through
            raise error.ValidationError("Empty pastes are not allowed")

        if any(len(L) != len(lexers) for L in [lexers, raws, filenames]):
            log.info("CreateAction.post: mismatching argument lists")
            raise error.ValidationError(
                "'lexers', 'raws', and 'filenames' arguments must be the same length"
            )

        if any(lexer not in utility.list_languages() for lexer in lexers):
            log.info("CreateAction.post: a file had an invalid lexer")
            raise error.ValidationError("Invalid lexer provided")

        with manager.DatabaseManager.get_session() as session, utility.SlugContext(
            auto_scale
        ) as slug_context:
            paste = models.Paste(
                next(slug_context), configuration.expiries[expiry], "web"
            )

            for lexer, raw, filename in zip(lexers, raws, filenames):
                paste.files.append(
                    models.File(
                        next(slug_context),
                        raw,
                        lexer,
                        filename if filename else None,
                    )
                )

            total_size = sum(len(f.fmt) for f in paste.files)
            if total_size > configuration.paste_size:
                log.info("CreateAction.post: sum of files was too large")
                raise error.ValidationError(
                    "Sum of file sizes exceeds size limit when syntax highlighting applied "
                    f"({total_size//1024}kB > {configuration.paste_size//1024}kB)"
                )

            # For the first file we will always use the same slug as the paste,
            # since slugs are generated to be unique over both pastes and files
            # this can be done safely.
            paste.files[0].slug = paste.slug

            session.add(paste)
            session.commit()

            # The removal cookie is set for the specific path of the paste it is
            # related to
            self.set_cookie(
                "removal", str(paste.removal), path=f"/{paste.slug}"
            )

            # Send the client to the paste
            self.redirect(f"/{paste.slug}")


class Repaste(Base):
    """Repaste is a specific case of the paste page. It only works for pre-
    existing pastes and will prefill the textarea and lexer."""

    @defensive.ratelimit(area="read")
    async def get(self, slug: str) -> None:  # type: ignore
        """Render the new paste form, optionally have a lexer preselected from
        the URL."""

        configuration: Configuration = ConfigurationProvider.get_config()

        with manager.DatabaseManager.get_session() as session:
            paste = (
                session.query(models.Paste)
                .filter(models.Paste.slug == slug)
                .first()
            )

            if not paste:
                raise tornado.web.HTTPError(404)

            lexers_available = utility.list_languages()

            await self.render(
                "create.html",
                expiries=configuration.expiries,
                lexers=["text"],  # XXX make this majority of file lexers?
                lexers_available=lexers_available,
                pagetitle="repaste",
                message=None,
                paste=paste,
            )


class Show(Base):
    """Show a paste."""

    @defensive.ratelimit(area="read")
    async def get(self, slug: str) -> None:  # type: ignore
        """Fetch paste from database by slug and render the paste."""

        with manager.DatabaseManager.get_session() as session:
            paste = (
                session.query(models.Paste)
                .filter(models.Paste.slug == slug)
                .first()
            )

            if not paste:
                raise tornado.web.HTTPError(404)

            if paste.exp_date < datetime.now(timezone.utc):
                session.delete(paste)
                session.commit()

                log.warning(
                    "Show.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            can_delete = self.get_cookie("removal") == str(paste.removal)

            self.render(
                "show.html",
                paste=paste,
                pagetitle=f"View paste {paste.slug}",
                can_delete=can_delete,
                linenos=False,
            )


class RedirectShow(Base):
    """Redirect old-style "/show/" paths to new-style "/" paths."""

    async def get(self, slug: str) -> None:  # type: ignore
        """Fetch paste from database and redirect to /slug if the paste
        exists."""
        with manager.DatabaseManager.get_session() as session:
            paste = (
                session.query(models.Paste)
                .filter(models.Paste.slug == slug)
                .first()
            )

            if not paste:
                raise tornado.web.HTTPError(404)

            if paste.exp_date < datetime.now(timezone.utc):
                session.delete(paste)
                session.commit()

                log.warning(
                    "RedirectShow.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            self.redirect(f"/{paste.slug}")


class FileRaw(Base):
    """Show a file as plaintext."""

    @defensive.ratelimit(area="read")
    async def get(self, file_id: str) -> None:  # type: ignore
        """Get a file from the database and show it in the plain."""

        with manager.DatabaseManager.get_session() as session:
            file = (
                session.query(models.File)
                .filter(models.File.slug == file_id)
                .first()
            )

            if not file:
                raise tornado.web.HTTPError(404)

            if file.paste.exp_date < datetime.now(timezone.utc):
                session.delete(file.paste)
                session.commit()

                log.warning(
                    "FileRaw.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            self.set_header("Content-Type", "text/plain; charset=utf-8")
            self.write(file.raw)


class FileHex(Base):
    """Show a file as hexadecimal."""

    @defensive.ratelimit(area="read")
    async def get(self, file_id: str) -> None:  # type: ignore
        """Get a file from the database and show it in hex."""

        with manager.DatabaseManager.get_session() as session:
            file = (
                session.query(models.File)
                .filter(models.File.slug == file_id)
                .first()
            )

            if not file:
                raise tornado.web.HTTPError(404)

            if file.paste.exp_date < datetime.now(timezone.utc):
                session.delete(file.paste)
                session.commit()

                log.warning(
                    "FileRaw.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            self.set_header("Content-Type", "text/plain; charset=utf-8")
            self.write(binascii.hexlify(file.raw.encode("utf8")))


class PasteDownload(Base):
    """Download an entire paste."""

    @defensive.ratelimit(area="read")
    async def get(self, paste_id: str) -> None:  # type: ignore
        """Get all files from the database and download them as a zipfile."""

        with manager.DatabaseManager.get_session() as session:
            paste = (
                session.query(models.Paste)
                .filter(models.Paste.slug == paste_id)
                .first()
            )

            if not paste:
                raise tornado.web.HTTPError(404)

            if paste.exp_date < datetime.now(timezone.utc):
                session.delete(paste)
                session.commit()

                log.warning(
                    "FileRaw.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            data = io.BytesIO()

            with zipfile.ZipFile(data, "x") as zf:
                for file in paste.files:
                    if file.filename:
                        filename = f"{utility.filename_clean(file.filename)}-{file.slug}.txt"
                    else:
                        filename = f"{file.slug}.txt"

                    zf.writestr(filename, file.raw)

            data.seek(0)

            self.set_header("Content-Type", "application/zip")
            self.set_header(
                "Content-Disposition", f"attachment; filename={paste.slug}.zip"
            )
            self.write(data.read())


class FileDownload(Base):
    """Download a file."""

    @defensive.ratelimit(area="read")
    async def get(self, file_id: str) -> None:  # type: ignore
        """Get a file from the database and download it in the plain."""

        with manager.DatabaseManager.get_session() as session:
            file = (
                session.query(models.File)
                .filter(models.File.slug == file_id)
                .first()
            )

            if not file:
                raise tornado.web.HTTPError(404)

            if file.paste.exp_date < datetime.now(timezone.utc):
                session.delete(file.paste)
                session.commit()

                log.warning(
                    "FileDownload.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            self.set_header("Content-Type", "text/plain; charset=utf-8")

            if file.filename:
                filename = (
                    f"{utility.filename_clean(file.filename)}-{file.slug}.txt"
                )
            else:
                filename = f"{file.slug}.txt"

            self.set_header(
                "Content-Disposition", f"attachment; filename={filename}"
            )
            self.write(file.raw)


class Remove(Base):
    """Remove a paste."""

    @defensive.ratelimit(area="delete")
    async def get(self, removal: str) -> None:  # type: ignore
        """Look up if the user visiting this page has the removal id for a
        certain paste. If they do they're authorized to remove the paste."""

        with manager.DatabaseManager.get_session() as session:
            paste = (
                session.query(models.Paste)
                .filter(models.Paste.removal == removal)
                .first()
            )

            if not paste:
                log.info("RemovePaste.get: someone visited with invalid id")
                raise tornado.web.HTTPError(404)

            if paste.exp_date < datetime.now(timezone.utc):
                session.delete(paste)
                session.commit()

                log.warning(
                    "Remove.get: paste was expired, is your cronjob running?"
                )

                raise tornado.web.HTTPError(404)

            session.delete(paste)
            session.commit()

        self.redirect("/")


class RestructuredTextPage(Base):
    """Render a given file as RestructuredText."""

    def initialize(self, file: str) -> None:
        self.file = file

    @defensive.ratelimit(area="read")
    async def get(self) -> None:
        try:
            with open(path.page / self.file) as f:
                html = docutils.core.publish_parts(
                    f.read(), writer_name="html"
                )["html_body"]
        except FileNotFoundError:
            raise tornado.web.HTTPError(404)

        self.render(
            "restructuredtextpage.html",
            html=html,
            pagetitle=(path.page / self.file).stem,
        )


class Logo(Base):
    """Render an image file at the logo path."""

    def initialize(self, path: str) -> None:
        self.path = path

    async def get(self) -> None:
        try:
            with open(self.path, "rb") as f:
                self.write(f.read())
                self.set_header("Content-Type", "image/png")
        except FileNotFoundError:
            raise tornado.web.HTTPError(404)
