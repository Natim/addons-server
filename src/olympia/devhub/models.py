import json
import string
import uuid
from copy import copy
from datetime import datetime

from django.apps import apps
from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models
from django.utils.translation import ugettext as _

import commonware.log
import jinja2

from olympia import amo
from olympia.amo.models import ModelBase, ManagerBase
from olympia.access.models import Group
from olympia.addons.models import Addon
from olympia.bandwagon.models import Collection
from olympia.files.models import File
from olympia.reviews.models import Review
from olympia.tags.models import Tag
from olympia.users.helpers import user_link
from olympia.users.models import UserProfile
from olympia.versions.models import Version


log = commonware.log.getLogger('devhub')


class RssKey(models.Model):
    # TODO: Convert to `models.UUIDField` but apparently we have a max_length
    # of 36 defined in the database and maybe store things with a hyphen
    # or maybe not...
    key = models.CharField(
        db_column='rsskey', max_length=36,
        default=lambda: uuid.uuid4().hex, unique=True)
    addon = models.ForeignKey(Addon, null=True, unique=True)
    user = models.ForeignKey(UserProfile, null=True, unique=True)
    created = models.DateField(default=datetime.now)

    class Meta:
        db_table = 'hubrsskeys'


class BlogPost(ModelBase):
    title = models.CharField(max_length=255)
    date_posted = models.DateField(default=datetime.now)
    permalink = models.CharField(max_length=255)

    class Meta:
        db_table = 'blogposts'


class AddonLog(ModelBase):
    """
    This table is for indexing the activity log by addon.
    """
    addon = models.ForeignKey(Addon)
    activity_log = models.ForeignKey('ActivityLog')

    class Meta:
        db_table = 'log_activity_addon'
        ordering = ('-created',)


class CommentLog(ModelBase):
    """
    This table is for indexing the activity log by comment.
    """
    activity_log = models.ForeignKey('ActivityLog')
    comments = models.CharField(max_length=255)

    class Meta:
        db_table = 'log_activity_comment'
        ordering = ('-created',)


class VersionLog(ModelBase):
    """
    This table is for indexing the activity log by version.
    """
    activity_log = models.ForeignKey('ActivityLog')
    version = models.ForeignKey(Version)

    class Meta:
        db_table = 'log_activity_version'
        ordering = ('-created',)


class UserLog(ModelBase):
    """
    This table is for indexing the activity log by user.
    Note: This includes activity performed unto the user.
    """
    activity_log = models.ForeignKey('ActivityLog')
    user = models.ForeignKey(UserProfile)

    class Meta:
        db_table = 'log_activity_user'
        ordering = ('-created',)


class GroupLog(ModelBase):
    """
    This table is for indexing the activity log by access group.
    """
    activity_log = models.ForeignKey('ActivityLog')
    group = models.ForeignKey(Group)

    class Meta:
        db_table = 'log_activity_group'
        ordering = ('-created',)


class ActivityLogManager(ManagerBase):
    def for_addons(self, addons):
        if isinstance(addons, Addon):
            addons = (addons,)

        vals = (AddonLog.objects.filter(addon__in=addons)
                .values_list('activity_log', flat=True))

        if vals:
            return self.filter(pk__in=list(vals))
        else:
            return self.none()

    def for_version(self, version):
        vals = (VersionLog.objects.filter(version=version)
                .values_list('activity_log', flat=True))
        return self.filter(pk__in=list(vals))

    def for_group(self, group):
        return self.filter(grouplog__group=group)

    def for_user(self, user):
        vals = (UserLog.objects.filter(user=user)
                .values_list('activity_log', flat=True))
        return self.filter(pk__in=list(vals))

    def for_developer(self):
        return self.exclude(action__in=amo.LOG_ADMINS + amo.LOG_HIDE_DEVELOPER)

    def admin_events(self):
        return self.filter(action__in=amo.LOG_ADMINS)

    def editor_events(self):
        return self.filter(action__in=amo.LOG_EDITORS)

    def review_queue(self):
        qs = self._by_type()
        return (qs.filter(action__in=amo.LOG_REVIEW_QUEUE)
                  .exclude(user__id=settings.TASK_USER_ID))

    def review_log(self):
        qs = self._by_type()
        return (qs.filter(action__in=amo.LOG_EDITOR_REVIEW_ACTION)
                  .exclude(user__id=settings.TASK_USER_ID))

    def beta_signed_events(self):
        """List of all the auto signatures of beta files."""
        # Even though we don't use BETA_SIGNED_VALIDATION_FAILED anymore, some
        # old logs might have it.
        return self.filter(action__in=[
            amo.LOG.BETA_SIGNED.id,
            amo.LOG.BETA_SIGNED_VALIDATION_FAILED.id])

    def total_reviews(self, theme=False):
        """Return the top users, and their # of reviews."""
        qs = self._by_type()
        return (qs.values('user', 'user__display_name', 'user__username')
                  .filter(action__in=([amo.LOG.THEME_REVIEW.id] if theme
                                      else amo.LOG_EDITOR_REVIEW_ACTION))
                  .exclude(user__id=settings.TASK_USER_ID)
                  .annotate(approval_count=models.Count('id'))
                  .order_by('-approval_count'))

    def monthly_reviews(self, theme=False):
        """Return the top users for the month, and their # of reviews."""
        qs = self._by_type()
        now = datetime.now()
        created_date = datetime(now.year, now.month, 1)
        return (qs.values('user', 'user__display_name', 'user__username')
                  .filter(created__gte=created_date,
                          action__in=([amo.LOG.THEME_REVIEW.id] if theme
                                      else amo.LOG_EDITOR_REVIEW_ACTION))
                  .exclude(user__id=settings.TASK_USER_ID)
                  .annotate(approval_count=models.Count('id'))
                  .order_by('-approval_count'))

    def user_approve_reviews(self, user):
        qs = self._by_type()
        return qs.filter(action__in=amo.LOG_EDITOR_REVIEW_ACTION,
                         user__id=user.id)

    def current_month_user_approve_reviews(self, user):
        now = datetime.now()
        ago = datetime(now.year, now.month, 1)
        return self.user_approve_reviews(user).filter(created__gte=ago)

    def user_position(self, values_qs, user):
        try:
            return next(i for (i, d) in enumerate(list(values_qs))
                        if d.get('user') == user.id) + 1
        except StopIteration:
            return None

    def total_reviews_user_position(self, user, theme=False):
        return self.user_position(self.total_reviews(theme), user)

    def monthly_reviews_user_position(self, user, theme=False):
        return self.user_position(self.monthly_reviews(theme), user)

    def _by_type(self):
        qs = super(ActivityLogManager, self).get_queryset()
        table = 'log_activity_addon'
        return qs.extra(
            tables=[table],
            where=['%s.activity_log_id=%s.id'
                   % (table, 'log_activity')])


class SafeFormatter(string.Formatter):
    """A replacement for str.format that escapes interpolated values."""

    def get_field(self, *args, **kw):
        # obj is the value getting interpolated into the string.
        obj, used_key = super(SafeFormatter, self).get_field(*args, **kw)
        return jinja2.escape(obj), used_key


class ActivityLog(ModelBase):
    TYPES = sorted([(value.id, key) for key, value in amo.LOG.items()])
    user = models.ForeignKey('users.UserProfile', null=True)
    action = models.SmallIntegerField(choices=TYPES, db_index=True)
    _arguments = models.TextField(blank=True, db_column='arguments')
    _details = models.TextField(blank=True, db_column='details')
    objects = ActivityLogManager()

    formatter = SafeFormatter()

    class Meta:
        db_table = 'log_activity'
        ordering = ('-created',)

    def f(self, *args, **kw):
        """Calls SafeFormatter.format and returns a Markup string."""
        # SafeFormatter escapes everything so this is safe.
        return jinja2.Markup(self.formatter.format(*args, **kw))

    @property
    def arguments(self):

        try:
            # d is a structure:
            # ``d = [{'addons.addon':12}, {'addons.addon':1}, ... ]``
            d = json.loads(self._arguments)
        except:
            log.debug('unserializing data from addon_log failed: %s' % self.id)
            return None

        objs = []
        for item in d:
            # item has only one element.
            model_name, pk = item.items()[0]
            if model_name in ('str', 'int', 'null'):
                objs.append(pk)
            else:
                (app_label, model_name) = model_name.split('.')
                model = apps.get_model(app_label, model_name)
                # Cope with soft deleted models and unlisted addons.
                objs.extend(model.get_unfiltered_manager().filter(pk=pk))

        return objs

    @arguments.setter
    def arguments(self, args=None):
        """
        Takes an object or a tuple of objects and serializes them and stores it
        in the db as a json string.
        """
        if args is None:
            args = []

        if not isinstance(args, (list, tuple)):
            args = (args,)

        serialize_me = []

        for arg in args:
            if isinstance(arg, basestring):
                serialize_me.append({'str': arg})
            elif isinstance(arg, (int, long)):
                serialize_me.append({'int': arg})
            elif isinstance(arg, tuple):
                # Instead of passing an addon instance you can pass a tuple:
                # (Addon, 3) for Addon with pk=3
                serialize_me.append(dict(((unicode(arg[0]._meta), arg[1]),)))
            else:
                serialize_me.append(dict(((unicode(arg._meta), arg.pk),)))

        self._arguments = json.dumps(serialize_me)

    @property
    def details(self):
        if self._details:
            return json.loads(self._details)

    @details.setter
    def details(self, data):
        self._details = json.dumps(data)

    @property
    def log(self):
        return amo.LOG_BY_ID[self.action]

    def to_string(self, type_=None):
        log_type = amo.LOG_BY_ID[self.action]
        if type_ and hasattr(log_type, '%s_format' % type_):
            format = getattr(log_type, '%s_format' % type_)
        else:
            format = log_type.format

        # We need to copy arguments so we can remove elements from it
        # while we loop over self.arguments.
        arguments = copy(self.arguments)
        addon = None
        review = None
        version = None
        collection = None
        tag = None
        group = None
        file_ = None

        for arg in self.arguments:
            if isinstance(arg, Addon) and not addon:
                if arg.has_listed_versions():
                    addon = self.f(u'<a href="{0}">{1}</a>',
                                   arg.get_url_path(), arg.name)
                else:
                    addon = self.f(u'{0}', arg.name)
                arguments.remove(arg)
            if isinstance(arg, Review) and not review:
                review = self.f(u'<a href="{0}">{1}</a>',
                                arg.get_url_path(), _('Review'))
                arguments.remove(arg)
            if isinstance(arg, Version) and not version:
                text = _('Version {0}')
                if arg.channel == amo.RELEASE_CHANNEL_LISTED:
                    version = self.f(u'<a href="{1}">%s</a>' % text,
                                     arg.version, arg.get_url_path())
                else:
                    version = self.f(text, arg.version)
                arguments.remove(arg)
            if isinstance(arg, Collection) and not collection:
                collection = self.f(u'<a href="{0}">{1}</a>',
                                    arg.get_url_path(), arg.name)
                arguments.remove(arg)
            if isinstance(arg, Tag) and not tag:
                if arg.can_reverse():
                    tag = self.f(u'<a href="{0}">{1}</a>',
                                 arg.get_url_path(), arg.tag_text)
                else:
                    tag = self.f('{0}', arg.tag_text)
            if isinstance(arg, Group) and not group:
                group = arg.name
                arguments.remove(arg)
            if isinstance(arg, File) and not file_:
                validation = 'passed'
                if self.action in (
                        amo.LOG.BETA_SIGNED.id,
                        amo.LOG.BETA_SIGNED_VALIDATION_FAILED.id,
                        amo.LOG.UNLISTED_SIGNED.id,
                        amo.LOG.UNLISTED_SIGNED_VALIDATION_FAILED.id):
                    validation = 'ignored'

                file_ = self.f(u'<a href="{0}">{1}</a> (validation {2})',
                               reverse('files.list', args=[arg.pk]),
                               arg.filename,
                               validation)
                arguments.remove(arg)

        user = user_link(self.user)

        try:
            kw = dict(addon=addon, review=review, version=version,
                      collection=collection, tag=tag, user=user, group=group,
                      file=file_)
            return self.f(format, *arguments, **kw)
        except (AttributeError, KeyError, IndexError):
            log.warning('%d contains garbage data' % (self.id or 0))
            return 'Something magical happened.'

    def __unicode__(self):
        return self.to_string()

    def __html__(self):
        return self
