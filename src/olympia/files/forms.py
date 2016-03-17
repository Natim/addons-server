from collections import defaultdict

from django import forms
from django.forms import widgets
from django.utils.translation import ugettext as _

import commonware.log
import happyforms
import jinja2

from olympia import amo
from olympia.files.models import File
from olympia.versions.models import Version

log = commonware.log.getLogger('z.files')


class FileSelectWidget(widgets.Select):
    def render_options(self, choices, selected_choices):
        def option(files, label=None, deleted=False):
            # Make sure that if there's a non-disabled version,
            # that's the one we use for the ID.
            files.sort(lambda a, b: ((a.status == amo.STATUS_DISABLED) -
                                     (b.status == amo.STATUS_DISABLED)))

            if label is None:
                label = u', '.join(f.get_platform_display() for f in files)

            output = [u'<option value="', jinja2.escape(files[0].id), u'" ']
            if selected in files:
                output.append(u' selected="true"')

            status = set(u'status-%s' % amo.STATUS_CHOICES_API[f.status]
                         for f in files)
            if deleted:
                status.update([u'status-deleted'])
            output.extend((u' class="', jinja2.escape(' '.join(status)), u'"'))
            output.extend((u'>', jinja2.escape(label), u'</option>\n'))
            return output

        if selected_choices[0]:
            selected = File.objects.get(id=selected_choices[0])
        else:
            selected = None

        file_ids = [int(c[0]) for c in self.choices if c[0]]

        output = []
        output.append(u'<option></option>')

        vers = Version.unfiltered.filter(files__id__in=file_ids).distinct()
        for ver in vers.order_by('-created'):
            hashes = defaultdict(list)
            for f in ver.files.filter(id__in=file_ids):
                hashes[f.hash].append(f)

            label = '{0} ({1})'.format(ver.version, ver.nomination)
            distinct_files = hashes.values()
            if len(distinct_files) == 1:
                output.extend(option(distinct_files[0], label, ver.deleted))
            elif distinct_files:
                output.extend((u'<optgroup label="',
                               jinja2.escape(ver.version), u'">'))
                for f in distinct_files:
                    output.extend(option(f, deleted=ver.deleted))
                output.append(u'</optgroup>')

        return jinja2.Markup(u''.join(output))


class FileCompareForm(happyforms.Form):
    left = forms.ModelChoiceField(queryset=File.objects.all(),
                                  widget=FileSelectWidget)
    right = forms.ModelChoiceField(queryset=File.objects.all(),
                                   widget=FileSelectWidget, required=False)

    def __init__(self, *args, **kw):
        self.addon = kw.pop('addon')
        super(FileCompareForm, self).__init__(*args, **kw)

        queryset = (File.objects.filter(version__addon=self.addon)
                        .exclude(status=amo.STATUS_BETA))
        self.fields['left'].queryset = queryset
        self.fields['right'].queryset = queryset

    def clean(self):
        if (not self.errors and
                self.cleaned_data.get('right') == self.cleaned_data['left']):
            raise forms.ValidationError(
                _('Cannot diff a version against itself'))
        return self.cleaned_data
