import re
from datetime import date

from django.core.validators import URLValidator
from django.forms import ValidationError
from django.utils.translation import get_language

from django_jinja import library
import jinja2

from mozillians.users import get_languages_for_locale


PARAGRAPH_RE = re.compile(r'(?:\r\n|\r|\n){2,}')


@library.filter
def paragraphize(value):
    return jinja2.Markup(u'\n\n'.join(u'<p>%s</p>' % p.replace('\n', '<br>\n')
                                      for p in PARAGRAPH_RE.split(jinja2.escape(value))))


@jinja2.contextfunction
@library.global_function
@library.render_with('includes/search_result.html')
def search_result(context, result):
    d = dict(context.items())
    d.update(result=result)
    return d


@library.global_function
def get_mozillian_years(userprofile):
    if userprofile.date_mozillian:
        year_difference = date.today().year - userprofile.date_mozillian.year
        return year_difference
    return None


@library.global_function
def langcode_to_name(code, locale=None):
    """Return the language name for the code in locale.

    If locale is None return in current activated language.
    """

    if not locale:
        locale = get_language()
    translated_languages = get_languages_for_locale(locale)
    try:
        lang = dict(translated_languages)[code]
    except KeyError:
        return code
    return lang


@library.filter
def simple_urlize(value):
    """Converts a string to a clickable link. If the string is legitimate
    URL address it returns a clickable link otherwise returns the
    string itself.

    """

    validate_url = URLValidator()

    try:
        validate_url(value)
    except ValidationError:
        return value

    return jinja2.Markup('<a href="%s">%s</a>' % (value, value))
