import json
import mimetypes
import os
from itertools import chain
from typing import List
from zipfile import BadZipfile, ZipFile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.forms import BaseModelFormSet, CharField, ChoiceField, FileField, HiddenInput, ModelForm, NumberInput, \
    Select, formset_factory
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import DetailView

from judge.highlight_code import highlight_code
from judge.models import CUSTOM_CHECKER_CPP, CUSTOM_CHECKER_PY, Problem, ProblemData, ProblemTestCase, Submission, \
    problem_data_storage
from judge.utils.problem_data import ProblemDataCompiler
from judge.utils.unicode import utf8text
from judge.utils.views import TitleMixin, add_file_response
from judge.views.problem import ProblemMixin

mimetypes.init()
mimetypes.add_type('application/x-yaml', '.yml')


def checker_args_cleaner(self):
    data = self.cleaned_data['checker_args']
    if not data or data.isspace():
        return ''
    try:
        if not isinstance(json.loads(data), dict):
            raise ValidationError(_('Checker arguments must be a JSON object.'))
    except ValueError:
        raise ValidationError(_('Checker arguments is invalid JSON.'))
    return data


class ProblemDataForm(ModelForm):
    checker_file = FileField(
        label=_('Custom checker source file'),
        help_text=_('Upload a checker source file for the selected custom checker.'),
        required=False,
    )
    checker_type = ChoiceField(
        label=_('Custom checker type'),
        choices=(
            ('testlib', 'testlib'),
            ('themis', 'themis'),
        ),
        required=False,
    )
    input_name = CharField(label=_('Input name'), required=False)
    output_name = CharField(label=_('Output name'), required=False)

    def __init__(self, *args, **kwargs):
        super(ProblemDataForm, self).__init__(*args, **kwargs)
        choices = list(self.fields['checker'].choices)
        choices.extend((
            (CUSTOM_CHECKER_PY, _('Custom checker (PY)')),
            (CUSTOM_CHECKER_CPP, _('Custom checker (CPP)')),
        ))
        self.fields['checker'].choices = choices
        self.order_fields([
            'zipfile', 'generator', 'unicode', 'nobigmath', 'output_limit', 'output_prefix',
            'checker', 'checker_type', 'input_name', 'output_name', 'checker_file', 'checker_args',
        ])
        if not self.is_bound and self.instance.pk:
            checker_py_exists = problem_data_storage.exists(
                os.path.join(self.instance.problem.code, 'checker.py'),
            )
            checker_cpp_exists = problem_data_storage.exists(
                os.path.join(self.instance.problem.code, 'checker.cpp'),
            )
            if checker_cpp_exists:
                self.initial['checker'] = CUSTOM_CHECKER_CPP
            elif checker_py_exists:
                self.initial['checker'] = CUSTOM_CHECKER_PY

            if self.initial.get('checker') == CUSTOM_CHECKER_CPP:
                self.fields['checker_file'].widget.attrs['accept'] = '.cpp'
                try:
                    checker_args = json.loads(self.instance.checker_args or '{}')
                except ValueError:
                    checker_args = {}
                self.initial['checker_type'] = checker_args.get('type', '')
                self.initial['input_name'] = checker_args.get('input_name', '')
                self.initial['output_name'] = checker_args.get('output_name', '')
            else:
                self.fields['checker_file'].widget.attrs['accept'] = '.py'

    def clean(self):
        cleaned_data = super(ProblemDataForm, self).clean()
        checker_file = cleaned_data.get('checker_file')
        checker = cleaned_data.get('checker')
        self.selected_checker = checker
        if checker == CUSTOM_CHECKER_PY:
            if not checker_file and not problem_data_storage.exists(
                os.path.join(self.instance.problem.code, 'checker.py'),
            ):
                self.add_error('checker_file', _('Upload a Python checker file to use the custom checker.'))
            if checker_file and not checker_file.name.lower().endswith('.py'):
                self.add_error('checker_file', _('Python checker files must end in .py.'))
            cleaned_data['checker_args'] = ''
            cleaned_data['checker'] = 'standard'
        elif checker == CUSTOM_CHECKER_CPP:
            if not checker_file and not problem_data_storage.exists(
                os.path.join(self.instance.problem.code, 'checker.cpp'),
            ):
                self.add_error('checker_file', _('Upload a C++ checker file to use the custom checker.'))
            if checker_file and not checker_file.name.lower().endswith(('.cc', '.cpp', '.cxx')):
                self.add_error('checker_file', _('C++ checker files must end in .cc, .cpp, or .cxx.'))

            checker_type = cleaned_data.get('checker_type')
            checker_args = {}
            if not checker_type:
                self.add_error('checker_type', _('Select a custom checker type.'))
            else:
                checker_args = {
                    'files': 'checker.cpp',
                    'lang': 'CPP17',
                    'type': checker_type,
                }
                if checker_type == 'themis':
                    input_name = (cleaned_data.get('input_name') or '').strip()
                    output_name = (cleaned_data.get('output_name') or '').strip()
                    if not input_name:
                        self.add_error('input_name', _('Input name is required for Themis checkers.'))
                    if not output_name:
                        self.add_error('output_name', _('Output name is required for Themis checkers.'))
                    checker_args['input_name'] = input_name
                    checker_args['output_name'] = output_name
            cleaned_data['checker_args'] = json.dumps(checker_args)
            cleaned_data['checker'] = 'standard'
        elif checker_file:
            self.add_error('checker_file', _('Select a custom checker before uploading a checker file.'))

        if checker not in (CUSTOM_CHECKER_PY, CUSTOM_CHECKER_CPP):
            checker_args = cleaned_data.get('checker_args') or ''
            try:
                parsed_checker_args = json.loads(checker_args) if checker_args else {}
            except ValueError:
                parsed_checker_args = {}
            if parsed_checker_args.get('files') == 'checker.cpp':
                cleaned_data['checker_args'] = ''
        return cleaned_data

    def clean_zipfile(self):
        if hasattr(self, 'zip_valid') and not self.zip_valid:
            raise ValidationError(_('Your zip file is invalid!'))

        zipfile = self.cleaned_data['zipfile']
        if zipfile and not zipfile.name.endswith('.zip'):
            raise ValidationError(_("Zip files must end in '.zip'"))

        return zipfile

    def clean_generator(self):
        generator = self.cleaned_data['generator']
        if generator and generator.name == 'init.yml':
            raise ValidationError(_('Generators must not be named init.yml.'))

        return generator

    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemData
        fields = ['zipfile', 'generator', 'unicode', 'nobigmath', 'output_limit', 'output_prefix',
                  'checker', 'checker_type', 'input_name', 'output_name', 'checker_file', 'checker_args']
        widgets = {
            'checker_args': HiddenInput,
        }


class ProblemCaseForm(ModelForm):
    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemTestCase
        fields = ('order', 'type', 'input_file', 'output_file', 'points', 'is_pretest', 'output_limit',
                  'output_prefix', 'checker', 'checker_args', 'generator_args', 'batch_dependencies')
        widgets = {
            'generator_args': HiddenInput,
            'batch_dependencies': HiddenInput,
            'type': Select(attrs={'style': 'width: 100%'}),
            'points': NumberInput(attrs={'style': 'width: 4em'}),
            'output_prefix': NumberInput(attrs={'style': 'width: 4.5em'}),
            'output_limit': NumberInput(attrs={'style': 'width: 6em'}),
            'checker_args': HiddenInput,
        }


class ProblemCaseFormSet(formset_factory(ProblemCaseForm, formset=BaseModelFormSet, extra=1, max_num=1,
                                         can_delete=True)):
    model = ProblemTestCase

    def __init__(self, *args, **kwargs):
        self.valid_files = kwargs.pop('valid_files', None)
        super(ProblemCaseFormSet, self).__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        form = super(ProblemCaseFormSet, self)._construct_form(i, **kwargs)
        form.valid_files = self.valid_files
        return form


class ProblemManagerMixin(LoginRequiredMixin, ProblemMixin, DetailView):
    def get_object(self, queryset=None):
        problem = super(ProblemManagerMixin, self).get_object(queryset)
        if problem.is_manually_managed:
            raise Http404()
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()


class ProblemSubmissionDiff(TitleMixin, ProblemMixin, DetailView):
    template_name = 'problem/submission-diff.html'

    def get_title(self):
        return _('Comparing submissions for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Comparing submissions for {0}')).format(
            format_html('<a href="{1}">{0}</a>', self.object.name, reverse('problem_detail', args=[self.object.code])),
        ))

    def get_object(self, queryset=None):
        problem = super(ProblemSubmissionDiff, self).get_object(queryset)
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionDiff, self).get_context_data(**kwargs)
        try:
            ids = self.request.GET.getlist('id')
            subs = Submission.objects.filter(id__in=ids)
        except ValueError:
            raise Http404
        if not subs:
            raise Http404

        context['submissions'] = subs

        # If we have associated data we can do better than just guess
        data = ProblemTestCase.objects.filter(dataset=self.object, type='C')
        if data:
            num_cases = data.count()
        else:
            num_cases = subs.first().test_cases.count()
        context['num_cases'] = num_cases
        return context


class ProblemDataView(TitleMixin, ProblemManagerMixin):
    template_name = 'problem/data.html'

    def get_title(self):
        return _('Editing data for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Editing data for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.object.name,
                        reverse('problem_detail', args=[self.object.code]))))

    def get_data_form(self, post=False):
        return ProblemDataForm(data=self.request.POST if post else None, prefix='problem-data',
                               files=self.request.FILES if post else None,
                               instance=ProblemData.objects.get_or_create(problem=self.object)[0])

    def get_case_formset(self, files, post=False):
        return ProblemCaseFormSet(data=self.request.POST if post else None, prefix='cases', valid_files=files,
                                  queryset=ProblemTestCase.objects.filter(dataset_id=self.object.pk).order_by('order'))

    def get_valid_files(self, data, post=False) -> List[str]:
        try:
            if post and 'problem-data-zipfile-clear' in self.request.POST:
                return []
            elif post and 'problem-data-zipfile' in self.request.FILES:
                return ZipFile(self.request.FILES['problem-data-zipfile']).namelist()
            elif data.zipfile:
                return ZipFile(data.zipfile.path).namelist()
        except BadZipfile:
            raise
        return []

    def get_context_data(self, **kwargs):
        context = super(ProblemDataView, self).get_context_data(**kwargs)
        valid_files = []
        if 'data_form' not in context:
            context['data_form'] = self.get_data_form()
            try:
                valid_files = self.get_valid_files(context['data_form'].instance)
            except BadZipfile:
                pass
        context['custom_checker_py_exists'] = problem_data_storage.exists(
            os.path.join(self.object.code, 'checker.py'),
        )
        context['custom_checker_cpp_exists'] = problem_data_storage.exists(
            os.path.join(self.object.code, 'checker.cpp'),
        )
        context['valid_files'] = set(valid_files)
        context['valid_files_json'] = mark_safe(json.dumps(valid_files))

        context['cases_formset'] = self.get_case_formset(valid_files)
        context['all_case_forms'] = chain(context['cases_formset'], [context['cases_formset'].empty_form])
        return context

    def post(self, request, *args, **kwargs):
        self.object = problem = self.get_object()
        data_form = self.get_data_form(post=True)
        try:
            valid_files = self.get_valid_files(data_form.instance, post=True)
            data_form.zip_valid = True
        except BadZipfile:
            valid_files = []
            data_form.zip_valid = False

        cases_formset = self.get_case_formset(valid_files, post=True)
        if data_form.is_valid() and cases_formset.is_valid():
            data = data_form.save()
            checker_file = data_form.cleaned_data['checker_file']
            if data_form.selected_checker == CUSTOM_CHECKER_PY:
                problem_data_storage.delete(os.path.join(problem.code, 'checker.cpp'))
                checker_path = os.path.join(problem.code, 'checker.py')
            elif data_form.selected_checker == CUSTOM_CHECKER_CPP:
                problem_data_storage.delete(os.path.join(problem.code, 'checker.py'))
                checker_path = os.path.join(problem.code, 'checker.cpp')
            else:
                problem_data_storage.delete(os.path.join(problem.code, 'checker.py'))
                problem_data_storage.delete(os.path.join(problem.code, 'checker.cpp'))
                checker_path = None

            if checker_file and checker_path:
                problem_data_storage.save(checker_path, checker_file)
            for case in cases_formset.save(commit=False):
                case.dataset_id = problem.id
                case.save()
            for case in cases_formset.deleted_objects:
                case.delete()
            ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order'), valid_files)
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(self.get_context_data(data_form=data_form, cases_formset=cases_formset,
                                                             valid_files=valid_files))

    put = post


@login_required
def problem_data_file(request, problem, path):
    object = get_object_or_404(Problem, code=problem)
    if not object.is_editable_by(request.user):
        raise Http404()

    problem_dir = problem_data_storage.path(problem)
    if os.path.commonpath((problem_data_storage.path(os.path.join(problem, path)), problem_dir)) != problem_dir:
        raise Http404()

    response = HttpResponse()

    if hasattr(settings, 'DMOJ_PROBLEM_DATA_INTERNAL'):
        url_path = '%s/%s/%s' % (settings.DMOJ_PROBLEM_DATA_INTERNAL, problem, path)
    else:
        url_path = None

    try:
        add_file_response(request, response, url_path, os.path.join(problem, path), problem_data_storage)
    except IOError:
        raise Http404()

    response['Content-Type'] = 'application/octet-stream'
    return response


@login_required
def problem_init_view(request, problem):
    problem = get_object_or_404(Problem, code=problem)
    if not problem.is_editable_by(request.user):
        raise Http404()

    try:
        with problem_data_storage.open(os.path.join(problem.code, 'init.yml'), 'rb') as f:
            data = utf8text(f.read()).rstrip('\n')
    except IOError:
        raise Http404()

    return render(request, 'problem/yaml.html', {
        'raw_source': data, 'highlighted_source': highlight_code(data, 'yaml'),
        'title': _('Generated init.yml for %s') % problem.name,
        'content_title': mark_safe(escape(_('Generated init.yml for %s')) % (
            format_html('<a href="{1}">{0}</a>', problem.name,
                        reverse('problem_detail', args=[problem.code])))),
    })
