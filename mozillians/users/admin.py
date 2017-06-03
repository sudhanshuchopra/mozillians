from socket import error as socket_error

from django import forms
from django.conf import settings
from django.conf.urls import patterns, url
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin import SimpleListFilter
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group, User
from django.core.urlresolvers import reverse
from django.db.models import Count, Q
from django.forms import ValidationError
from django.http import HttpResponseRedirect

from dal import autocomplete
from celery.task.sets import TaskSet
from functools import update_wrapper
from import_export.fields import Field
from import_export.resources import ModelResource
from sorl.thumbnail.admin import AdminImageMixin

from mozillians.common.mixins import MozilliansAdminExportMixin
from mozillians.common.templatetags.helpers import get_datetime
from mozillians.groups.admin import BaseGroupMembershipAutocompleteForm
from mozillians.groups.models import GroupMembership, Skill
from mozillians.users.models import get_languages_for_locale
from mozillians.users.models import (AbuseReport, ExternalAccount, Language, PUBLIC,
                                     UserProfile, UsernameBlacklist, Vouch)
from mozillians.users.tasks import (check_celery, subscribe_user_to_basket,
                                    unsubscribe_from_basket_task)


admin.site.unregister(Group)


Q_PUBLIC_PROFILES = Q()
for field in UserProfile.privacy_fields():
    key = 'privacy_%s' % field
    Q_PUBLIC_PROFILES |= Q(**{key: PUBLIC})


def subscribe_to_basket_action(newsletter):
    """Subscribe to Basket action."""

    def subscribe_to_basket(modeladmin, request, queryset):
        """Subscribe to Basket or update details of already subscribed."""
        ts = [(subscribe_user_to_basket.subtask(args=[userprofile.id, [newsletter]]))
              for userprofile in queryset]
        TaskSet(ts).apply_async()
        messages.success(request, 'Basket update started.')

    subscribe_to_basket.short_description = 'Subscribe to or Update {0}'.format(newsletter)
    subscribe_to_basket.__name__ = 'subscribe_to_basket_{0}'.format(newsletter.replace('-', '_'))
    return subscribe_to_basket


def unsubscribe_from_basket_action(newsletter):
    """Unsubscribe from Basket action."""

    def unsubscribe_from_basket(modeladmin, request, queryset):
        """Unsubscribe from Basket."""
        ts = [(unsubscribe_from_basket_task.subtask(args=[userprofile.user.email,
                                                          [newsletter]]))
              for userprofile in queryset]
        TaskSet(ts).apply_async()
        messages.success(request, 'Basket update started.')

    unsubscribe_from_basket.short_description = 'Unsubscribe from {0}'.format(newsletter)
    func_name = 'unsubscribe_from_basket_{0}'.format(newsletter.replace('-', '_'))
    unsubscribe_from_basket.__name__ = func_name
    return unsubscribe_from_basket


def update_vouch_flags_action():
    """Update can_vouch, is_vouched flag action."""

    def update_vouch_flags(modeladmin, request, queryset):
        for profile in queryset:
            vouches_received = profile.vouches_received.count()
            profile.can_vouch = vouches_received >= settings.CAN_VOUCH_THRESHOLD
            profile.is_vouched = vouches_received > 0
            profile.save()
    update_vouch_flags.short_description = 'Update vouch flags'
    return update_vouch_flags


class SuperUserFilter(SimpleListFilter):
    """Admin filter for superusers."""
    title = 'has access to admin interface'
    parameter_name = 'superuser'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset

        value = self.value() == 'True'
        return queryset.filter(user__is_staff=value)


class PublicProfileFilter(SimpleListFilter):
    """Admin filter for public profiles."""
    title = 'public profile'
    parameter_name = 'public_profile'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset

        if self.value() == 'True':
            return queryset.filter(Q_PUBLIC_PROFILES)

        return queryset.exclude(Q_PUBLIC_PROFILES)


class CompleteProfileFilter(SimpleListFilter):
    """Admin filter for complete profiles."""
    title = 'complete profile'
    parameter_name = 'complete_profile'

    def lookups(self, request, model_admin):
        return (('False', 'Incomplete'),
                ('True', 'Complete'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        elif self.value() == 'True':
            return queryset.exclude(full_name='')
        else:
            return queryset.filter(full_name='')


class DateJoinedFilter(SimpleListFilter):
    """Admin filter for date joined."""
    title = 'date joined'
    parameter_name = 'date_joined'

    def lookups(self, request, model_admin):
        join_dates = User.objects.values_list('date_joined', flat=True)
        join_years = [x.year for x in join_dates]
        return [(str(x), x) for x in set(join_years)]

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        else:
            return queryset.filter(user__date_joined__year=self.value())
        return queryset


class LastLoginFilter(SimpleListFilter):
    """Admin filter for last login."""
    title = 'last login'
    parameter_name = 'last_login'

    def lookups(self, request, model_admin):
        # Number is in days
        return (('<7', 'Less than a week'),
                ('<30', 'Less than a month'),
                ('<90', 'Less than 3 months'),
                ('<180', 'Less than 6 months'),
                ('>180', 'Between 6 and 12 months'),
                ('>360', 'More than a year'))

    def queryset(self, request, queryset):

        if self.value() == '<7':
            return queryset.filter(user__last_login__gte=get_datetime(-7))
        elif self.value() == '<30':
            return queryset.filter(user__last_login__gte=get_datetime(-30))
        elif self.value() == '<90':
            return queryset.filter(user__last_login__gte=get_datetime(-90))
        elif self.value() == '<180':
            return queryset.filter(user__last_login__gte=get_datetime(-180))
        elif self.value() == '>180':
            return queryset.filter(user__last_login__lt=get_datetime(-180),
                                   user__last_login__gt=get_datetime(-360))
        elif self.value() == '>360':
            return queryset.filter(user__last_login__lt=get_datetime(-360))
        return queryset


class AlternateEmailFilter(SimpleListFilter):
    """Admin filter for users with alternate emails."""
    title = 'alternate email'
    parameter_name = 'alternate_email'

    def lookups(self, request, model_admin):
        return(('False', 'No'), ('True', 'Yes'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset

        if self.value() == 'True':
            return queryset.filter(externalaccount__type=ExternalAccount.TYPE_EMAIL)

        return queryset.exclude(externalaccount__type=ExternalAccount.TYPE_EMAIL)


class LegacyVouchFilter(SimpleListFilter):
    """Admin filter for profiles with new or legacy vouch type."""
    title = 'vouch type'
    parameter_name = 'vouch_type'

    def lookups(self, request, model_admin):
        return (('legacy', 'Legacy'),
                ('new', 'New'))

    def queryset(self, request, queryset):
        vouched = queryset.filter(is_vouched=True)
        newvouches = (Vouch.objects
                      .exclude(description='')
                      .values_list('vouchee', flat=True)
                      .distinct())
        # Load into memory
        newvouches = list(newvouches)

        if self.value() == 'legacy':
            return vouched.exclude(pk__in=newvouches)
        elif self.value() == 'new':
            return vouched.filter(pk__in=newvouches)
        return queryset


class NDAMemberFilter(SimpleListFilter):
    """Admin filter for profiles member of the NDA group"""
    title = "NDA member"
    parameter_name = 'nda_member'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        from mozillians.groups.models import Group, GroupMembership

        try:
            group = Group.objects.get(name=settings.NDA_GROUP)
        except Group.DoesNotExist:
            return queryset

        memberships = GroupMembership.objects.filter(group=group, status=GroupMembership.MEMBER)
        profile_ids = memberships.values_list('userprofile__id', flat=True)

        if self.value() == 'False':
            return queryset.exclude(id__in=profile_ids)
        elif self.value() == 'True':
            return queryset.filter(id__in=profile_ids)
        return queryset


class BasketTokenFilter(SimpleListFilter):
    """Admin filter for profiles with associated basket token"""
    title = 'has basket token'
    parameter_name = 'basket_token'

    def lookups(self, request, model_admin):
        return (('yes', 'Yes'),
                ('no', 'No'))

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(basket_token='')
        elif self.value() == 'no':
            return queryset.filter(basket_token='')
        return queryset


class MissingCountry(SimpleListFilter):
    """Admin filter for profiles missing country information"""
    title = 'Missing country'
    parameter_name = 'missing_country'

    def lookups(self, request, model_admin):
        return (('both', 'Both geo_country/country'),
                ('geo_country', 'Only geo_country'),
                ('country', 'Only country'))

    def queryset(self, request, queryset):

        if self.value() == 'both':
            return queryset.filter(country__isnull=True, geo_country__isnull=True)
        elif self.value() == 'geo_country':
            return queryset.filter(geo_country__isnull=True)
        elif self.value() == 'country':
            return queryset.filter(country__isnull=True)
        return queryset


class MissingRegion(SimpleListFilter):
    """Admin filter for profiles missing region information"""
    title = 'Missing region'
    parameter_name = 'missing_region'

    def lookups(self, request, model_admin):
        return (('both', 'Both geo_region/region'),
                ('geo_region', 'Only geo_region'),
                ('region', 'Only region'))

    def queryset(self, request, queryset):

        if self.value() == 'both':
            return queryset.filter(region__isnull=True, geo_region__isnull=True)
        elif self.value() == 'geo_region':
            return queryset.filter(geo_region__isnull=True)
        elif self.value() == 'region':
            return queryset.filter(region__isnull=True)
        return queryset


class MissingCity(SimpleListFilter):
    """Admin filter for profiles missing city information"""
    title = 'Missing city'
    parameter_name = 'missing_city'

    def lookups(self, request, model_admin):
        return (('both', 'Both geo_city/city'),
                ('geo_city', 'Only geo_city'),
                ('city', 'Only city'))

    def queryset(self, request, queryset):

        if self.value() == 'both':
            return queryset.filter(city__isnull=True, geo_city__isnull=True)
        elif self.value() == 'geo_city':
            return queryset.filter(geo_city__isnull=True)
        elif self.value() == 'city':
            return queryset.filter(city__isnull=True)
        return queryset


class UsernameBlacklistAdmin(MozilliansAdminExportMixin, admin.ModelAdmin):
    """UsernameBlacklist Admin."""
    save_on_top = True
    search_fields = ['value']
    list_filter = ['is_regex']
    list_display = ['value', 'is_regex']


admin.site.register(UsernameBlacklist, UsernameBlacklistAdmin)


class MissingLanguagesFilter(SimpleListFilter):
    title = 'Missing language'
    parameter_name = 'missing_language'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        current_language_codes = set(Language.objects.values_list('code', flat=True))
        babel_language_codes = set([code for code, lang in get_languages_for_locale('en')])

        if self.value() == 'True':
            missing_language_codes = current_language_codes.difference(babel_language_codes)
            return queryset.filter(code__in=list(missing_language_codes))

        if self.value() == 'False':
            return queryset.filter(code__in=list(babel_language_codes))

        return queryset


class LanguageResource(ModelResource):
    """django-import-export Language resource."""
    email = Field(attribute='userprofile__user__email')

    class Meta:
        model = Language


class LanguageAdmin(MozilliansAdminExportMixin, admin.ModelAdmin):
    resource_class = LanguageResource
    search_fields = ['userprofile__full_name', 'userprofile__user__email', 'code']
    list_display = ['get_code', 'get_language_name', 'userprofile']
    list_filter = ['code', MissingLanguagesFilter]

    def get_code(self, obj):
        return obj.code
    get_code.short_description = 'Code'

    def get_language_name(self, obj):
        return obj.get_code_display()
    get_language_name.short_description = 'Name'


admin.site.register(Language, LanguageAdmin)


class SkillInline(admin.TabularInline):
    model = Skill
    extra = 1


class UserMembershipAutocompleteForm(BaseGroupMembershipAutocompleteForm):

    class Meta:
        widgets = {
            'group': autocomplete.ModelSelect2(url='groups:group-autocomplete'),
        }


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 1
    form = UserMembershipAutocompleteForm


class LanguageInline(admin.TabularInline):
    model = Language
    extra = 1


class ExternalAccountInline(admin.TabularInline):
    model = ExternalAccount
    extra = 1

    def queryset(self, request):
        """Exclude alternate emails from external accounts"""
        qs = super(ExternalAccountInline, self).queryset(request)
        return qs.exclude(type=ExternalAccount.TYPE_EMAIL)


class AlternateEmailForm(forms.ModelForm):
    def save(self, *args, **kwargs):
        self.instance.type = ExternalAccount.TYPE_EMAIL
        return super(AlternateEmailForm, self).save(*args, **kwargs)

    class Meta:
        model = ExternalAccount
        exclude = ['type']


class AlternateEmailInline(admin.TabularInline):
    form = AlternateEmailForm
    model = ExternalAccount
    extra = 1
    verbose_name = 'Alternate Email'
    verbose_name_plural = 'Alternate Emails'

    def queryset(self, request):
        """Limit queryset to alternate emails."""
        qs = super(AlternateEmailInline, self).queryset(request)
        return qs.filter(type=ExternalAccount.TYPE_EMAIL)


class UserProfileAdminForm(forms.ModelForm):
    username = forms.CharField()
    email = forms.CharField()
    last_login = forms.DateTimeField(required=False)
    date_joined = forms.DateTimeField(required=False)

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.get('instance')
        if self.instance:
            self.base_fields['username'].initial = self.instance.user.username
            self.base_fields['email'].initial = self.instance.user.email
        super(UserProfileAdminForm, self).__init__(*args, **kwargs)

    def clean_username(self):
        username = self.cleaned_data['username']
        if (User.objects.exclude(pk=self.instance.user.pk)
                .filter(username=username).exists()):
            raise ValidationError('Username already exists')
        return username

    def clean_email(self):
        email = self.cleaned_data['email']
        if (User.objects.exclude(pk=self.instance.user.pk)
                .filter(email=email).exists()):
            raise ValidationError('Email already exists')
        return email

    def save(self, *args, **kwargs):
        if self.instance:
            self.instance.user.username = self.cleaned_data.get('username')
            self.instance.user.email = self.cleaned_data.get('email')
            self.instance.user.save()
        return super(UserProfileAdminForm, self).save(*args, **kwargs)

    class Meta:
        model = UserProfile
        fields = '__all__'


class UserProfileResource(ModelResource):
    """django-import-export UserProfile Resource."""
    username = Field(attribute='user__username')
    email = Field(attribute='user__email')
    country_name = Field(attribute='geo_country__name')
    country_code = Field(attribute='geo_country__code')
    region_name = Field(attribute='geo_region__name')
    region_code = Field(attribute='geo_region__code')
    city_name = Field(attribute='geo_city__name')
    city_code = Field(attribute='geo_city__code')

    class Meta:
        model = UserProfile


class UserProfileAdmin(AdminImageMixin, MozilliansAdminExportMixin, admin.ModelAdmin):
    resource_class = UserProfileResource
    inlines = [LanguageInline, GroupMembershipInline, ExternalAccountInline,
               AlternateEmailInline]
    search_fields = ['full_name', 'user__email', 'user__username', 'ircname',
                     'geo_country__name', 'geo_region__name', 'geo_city__name']
    readonly_fields = ['date_vouched', 'vouched_by', 'user', 'date_joined', 'last_login',
                       'is_vouched', 'can_vouch', 'referral_source']
    form = UserProfileAdminForm
    list_filter = ['is_vouched', 'can_vouch', DateJoinedFilter,
                   LastLoginFilter, LegacyVouchFilter, SuperUserFilter,
                   CompleteProfileFilter, PublicProfileFilter, AlternateEmailFilter,
                   NDAMemberFilter, BasketTokenFilter, MissingCountry, MissingRegion,
                   MissingCity, 'externalaccount__type']
    save_on_top = True
    list_display = ['full_name', 'email', 'username', 'geo_country', 'is_vouched', 'can_vouch',
                    'number_of_vouchees', 'date_joined']
    list_display_links = ['full_name', 'email', 'username']
    actions = [subscribe_to_basket_action(settings.BASKET_VOUCHED_NEWSLETTER),
               unsubscribe_from_basket_action(settings.BASKET_VOUCHED_NEWSLETTER),
               subscribe_to_basket_action(settings.BASKET_NDA_NEWSLETTER),
               unsubscribe_from_basket_action(settings.BASKET_NDA_NEWSLETTER),
               update_vouch_flags_action()]

    fieldsets = (
        ('Account', {
            'fields': ('full_name', 'full_name_local', 'username', 'email', 'photo',)
        }),
        (None, {
            'fields': ('title', 'bio', 'tshirt', 'ircname', 'date_mozillian',)
        }),
        ('Important dates', {
            'fields': ('date_joined', 'last_login')
        }),
        ('Vouch Info', {
            'fields': ('date_vouched', 'is_vouched', 'can_vouch')
        }),
        ('Location', {
            'fields': ('geo_country', 'geo_region', 'geo_city',
                       'lng', 'lat', 'timezone')
        }),
        ('Privacy Settings', {
            'fields': ('privacy_photo', 'privacy_full_name', 'privacy_full_name_local',
                       'privacy_ircname', 'privacy_email', 'privacy_bio',
                       'privacy_geo_city', 'privacy_geo_region', 'privacy_geo_country',
                       'privacy_groups', 'privacy_skills', 'privacy_languages',
                       'privacy_date_mozillian', 'privacy_timezone',
                       'privacy_tshirt', 'privacy_title'),
            'classes': ('collapse',)
        }),
        ('Basket', {
            'fields': ('basket_token',),
            'classes': ('collapse',)
        }),
        ('Skills', {
            'fields': ('skills',)
        }),
    )

    def get_queryset(self, request):
        qs = super(UserProfileAdmin, self).get_queryset(request)
        qs = qs.annotate(vouches_made_count=Count('vouches_made'))
        return qs

    def email(self, obj):
        return obj.user.email
    email.admin_order_field = 'user__email'

    def username(self, obj):
        return obj.user.username
    username.admin_order_field = 'user__username'

    def is_vouched(self, obj):
        return obj.userprofile.is_vouched
    is_vouched.boolean = True
    is_vouched.admin_order_field = 'is_vouched'

    def vouched_by(self, obj):
        voucher = obj.vouched_by
        if voucher:
            voucher_url = reverse('admin:auth_user_change', args=[voucher.id])
            return '<a href="%s">%s</a>' % (voucher_url, voucher)
    vouched_by.admin_order_field = 'vouched_by'
    vouched_by.allow_tags = True

    def number_of_vouchees(self, obj):
        """Return the number of vouchees for obj."""
        return obj.vouches_made_count
    number_of_vouchees.admin_order_field = 'vouches_made_count'

    def last_login(self, obj):
        return obj.user.last_login

    def date_joined(self, obj):
        return obj.user.date_joined

    def get_actions(self, request):
        """Return bulk actions for UserAdmin without bulk delete."""
        actions = super(UserProfileAdmin, self).get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def check_celery(self, request):
        try:
            investigator = check_celery.delay()
        except socket_error as e:
            messages.error(request, 'Cannot connect to broker: %s' % e)
            return HttpResponseRedirect(reverse('admin:users_userprofile_changelist'))

        try:
            investigator.get(timeout=5)
        except investigator.TimeoutError as e:
            messages.error(request, 'Worker timeout: %s' % e)
        except Exception as e:
            raise e
        else:
            messages.success(request, 'Celery is OK')

        return HttpResponseRedirect(reverse('admin:users_userprofile_changelist'))

    def get_urls(self):
        """Return custom and UserProfileAdmin urls."""

        def wrap(view):

            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        urls = super(UserProfileAdmin, self).get_urls()
        my_urls = patterns(
            '',
            url(r'check_celery', wrap(self.check_celery), name='users_check_celery')
        )
        return my_urls + urls


admin.site.register(UserProfile, UserProfileAdmin)


class NullProfileFilter(SimpleListFilter):
    """Admin filter for null profiles."""
    title = 'has user profile'
    parameter_name = 'has_user_profile'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        value = self.value() != 'True'
        return queryset.filter(userprofile__isnull=value)


class UserAdmin(UserAdmin):
    list_filter = [NullProfileFilter]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


class GroupAdmin(MozilliansAdminExportMixin, GroupAdmin):
    pass


admin.site.register(Group, GroupAdmin)


class VouchAutocompleteForm(forms.ModelForm):

    class Meta:
        model = Vouch
        fields = '__all__'
        widgets = {
            'vouchee': autocomplete.ModelSelect2(url='users:vouchee-autocomplete'),
            'voucher': autocomplete.ModelSelect2(url='users:voucher-autocomplete')
        }


class VouchAdmin(admin.ModelAdmin):
    save_on_top = True
    search_fields = ['voucher__user__username', 'voucher__full_name',
                     'vouchee__user__username', 'vouchee__full_name',
                     'voucher__user__email', 'vouchee__user__email']
    list_display = ['vouchee', 'voucher', 'date', 'autovouch']
    list_filter = ['autovouch']
    form = VouchAutocompleteForm


admin.site.register(Vouch, VouchAdmin)


class AbuseReportAutocompleteForm(forms.ModelForm):

    class Meta:
        model = AbuseReport
        fields = '__all__'
        widgets = {
            'profile': autocomplete.ModelSelect2(url='users:vouchee-autocomplete'),
            'reporter': autocomplete.ModelSelect2(url='users:vouchee-autocomplete'),
        }


class AbuseReportAdmin(admin.ModelAdmin):
    form = AbuseReportAutocompleteForm
    list_display = ['profile', 'reporter', 'type', 'created', 'updated']
    list_filter = ['type', 'is_akismet']


admin.site.register(AbuseReport, AbuseReportAdmin)
