import datetime
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.urlresolvers import reverse
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import signals
from django.db.models.signals import post_delete, post_save
from django.template import loader, Context
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import get_language, ugettext_lazy as _

from aplus.api import api_reverse
from course.models import StudentGroup, CourseInstance, CourseModule, LearningObjectCategory
from external_services.lti import CustomStudentInfoLTIRequest
from external_services.models import LTIService
from inheritance.models import ModelWithInheritance
from lib.api.authentication import (
    get_graderauth_submission_params,
    get_graderauth_exercise_params,
)
from lib.fields import JSONField
from lib.helpers import (
    Enum,
    update_url_params,
    has_same_domain,
    safe_file_name,
    roman_numeral,
)
from lib.models import UrlMixin
from userprofile.models import UserProfile

from .cache.exercise import ExerciseCache
from .protocol.aplus import load_exercise_page, load_feedback_page
from .protocol.exercise_page import ExercisePage


class LearningObjectManager(models.Manager):

    def get_queryset(self):
        return super().get_queryset()\
            .defer('description')\
            .select_related('course_module', 'course_module__course_instance',
                'course_module__course_instance__course', 'category')

    def find_enrollment_exercise(self, course_instance, profile):
        exercise = None
        if profile.is_external:
            exercise = self.filter(
                course_module__course_instance=course_instance,
                status='enrollment_ext'
            ).first()
        return exercise or self.filter(
            course_module__course_instance=course_instance,
            status='enrollment'
        ).first()


class LearningObject(UrlMixin, ModelWithInheritance):
    """
    All learning objects inherit this model.
    """
    STATUS = Enum([
        ('READY', 'ready', _("Ready")),
        ('UNLISTED', 'unlisted', _("Unlisted in table of contents")),
        ('ENROLLMENT', 'enrollment', _("Enrollment questions")),
        ('ENROLLMENT_EXTERNAL', 'enrollment_ext', _("Enrollment questions for external students")),
        ('HIDDEN', 'hidden', _("Hidden from non course staff")),
        ('MAINTENANCE', 'maintenance', _("Maintenance")),
    ])
    AUDIENCE = Enum([
        ('COURSE_AUDIENCE', 0, _('Course audience')),
        ('INTERNAL_USERS', 1, _('Only internal users')),
        ('EXTERNAL_USERS', 2, _('Only external users')),
        ('REGISTERED_USERS', 3, _('Only registered users')),
    ])
    status = models.CharField(max_length=32,
        choices=STATUS.choices, default=STATUS.READY)
    audience = models.IntegerField(choices=AUDIENCE.choices,
        default=AUDIENCE.COURSE_AUDIENCE)
    category = models.ForeignKey(LearningObjectCategory, related_name="learning_objects")
    course_module = models.ForeignKey(CourseModule, related_name="learning_objects")
    parent = models.ForeignKey('self', on_delete=models.SET_NULL,
        blank=True, null=True, related_name='children')
    order = models.IntegerField(default=1)
    url = models.CharField(max_length=255,
        validators=[RegexValidator(regex="^[\w\-\.]*$")],
        help_text=_("Input an URL identifier for this object."))
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True,
        help_text=_("Internal description is not presented on site."))
    use_wide_column = models.BooleanField(default=False,
        help_text=_("Remove the third info column for more space."))

    service_url = models.URLField(blank=True)
    exercise_info = JSONField(blank=True)
    model_answers = models.TextField(blank=True,
        help_text=_("List model answer files as protected URL addresses."))
    templates = models.TextField(blank=True,
        help_text=_("List template files as protected URL addresses."))

    # Keep this to support ExerciseWithAttachment
    # Maybe this should inject extra content to any exercise
    content = models.TextField(blank=True)

    objects = LearningObjectManager()

    class Meta:
        app_label = "exercise"
        ordering = ['course_module', 'order', 'id']
        unique_together = ['course_module', 'parent', 'url']

    def clean(self):
        """
        Validates the model before saving (standard method used in Django admin).
        """
        super().clean()
        errors = {}
        RESERVED = ("submissions", "plain", "info")
        if self.url in RESERVED:
            errors['url'] = _("Taken words include: {}").format(", ".join(RESERVED))
        if self.course_module.course_instance != self.category.course_instance:
            errors['category'] = _('Course_module and category must belong to the same course instance.')
        if self.parent:
            if self.parent.course_module != self.course_module:
                errors['parent'] = _('Cannot select parent from another course module.')
            if self.parent.id == self.id:
                errors['parent'] = _('Cannot select self as a parent.')
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Trigger LearningObject post save signal for extending classes.
        cls = self.__class__
        while cls.__bases__:
            cls = cls.__bases__[0]
            if cls.__name__ == 'LearningObject':
                signals.post_save.send(sender=cls, instance=self)

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        # Trigger LearningObject post delete signal for extending classes.
        cls = self.__class__
        while cls.__bases__:
            cls = cls.__bases__[0]
            if cls.__name__ == 'LearningObject':
                signals.post_delete.send(sender=cls, instance=self)

    def __str__(self):
        if self.order >= 0:
            if self.course_instance.content_numbering == CourseInstance.CONTENT_NUMBERING.ARABIC:
                number = self.number()
                if self.course_instance.module_numbering in (
                        CourseInstance.CONTENT_NUMBERING.ARABIC,
                        CourseInstance.CONTENT_NUMBERING.HIDDEN,
                    ):
                    return "{:d}.{} {}".format(self.course_module.order, number, self.name)
                return "{} {}".format(number, self.name)
            elif self.course_instance.content_numbering == CourseInstance.CONTENT_NUMBERING.ROMAN:
                return "{} {}".format(roman_numeral(self.order), self.name)
        return self.name

    def number(self):
        return ".".join([str(o.order) for o in self.parent_list()])

    def parent_list(self):
        if not hasattr(self, '_parents'):
            def recursion(obj, parents):
                if not obj is None:
                    return recursion(obj.parent, [obj] + parents)
                return parents
            self._parents = recursion(self.parent, [self])
        return self._parents

    @property
    def course_instance(self):
        return self.course_module.course_instance

    @property
    def is_submittable(self):
        return False

    def is_empty(self):
        return not self.service_url and self.as_leaf_class()._is_empty()

    def _is_empty(self):
        return True

    def is_open(self, when=None):
        return self.course_module.is_open(when=when)

    def is_after_open(self, when=None):
        return self.course_module.is_after_open(when=when)

    def is_closed(self, when=None):
        return self.course_module.is_closed(when=when)

    @property
    def can_show_solutions(self):
        return self.is_closed() and not self.course_instance.is_on_lifesupport() and not self.course_instance.is_archived()

    def get_path(self):
        return "/".join([o.url for o in self.parent_list()])

    ABSOLUTE_URL_NAME = "exercise"

    def get_url_kwargs(self):
        return dict(exercise_path=self.get_path(), **self.course_module.get_url_kwargs())

    def get_display_url(self):
        if self.status == self.STATUS.UNLISTED and self.parent:
            return "{}#chapter-exercise-{:d}".format(
                self.parent_list()[-2].get_absolute_url(),
                self.order
            )
        return self.get_absolute_url()

    def get_submission_list_url(self):
        return self.get_url("submission-list")

    def load(self, request, students, url_name="exercise"):
        """
        Loads the learning object page.
        """
        page = ExercisePage(self)
        if not self.service_url:
            return page
        language = get_language()
        cache = ExerciseCache(self, language, request, students, url_name)
        page.head = cache.head()
        page.content = cache.content()
        page.is_loaded = True
        return page

    def load_page(self, language, request, students, url_name, last_modified=None):
        return load_exercise_page(
            request,
            self.get_load_url(language, request, students, url_name),
            last_modified,
            self
        )

    def get_load_url(self, language, request, students, url_name="exercise"):
        return update_url_params(self.service_url, {
            'lang': language,
        })

    def get_models(self):
        return [(url,url.split('/')[-1]) for url in self.model_answers.split()]

    def get_templates(self):
        return [(url,url.split('/')[-1]) for url in self.templates.split()]


def invalidate_exercise(sender, instance, **kwargs):
    for language,_ in settings.LANGUAGES:
        ExerciseCache.invalidate(instance, modifiers=[language])


# Automatically invalidate cached exercise html when edited.
post_save.connect(invalidate_exercise, sender=LearningObject)
post_delete.connect(invalidate_exercise, sender=LearningObject)


class LearningObjectDisplay(models.Model):
    """
    Records views of learning objects.
    """
    learning_object = models.ForeignKey(LearningObject)
    profile = models.ForeignKey(UserProfile)
    timestamp = models.DateTimeField(auto_now_add=True)


class CourseChapter(LearningObject):
    """
    Chapters can offer and organize learning material as one page chapters.
    """
    generate_table_of_contents = models.BooleanField(default=False)

    def _is_empty(self):
        return not self.generate_table_of_contents


class BaseExercise(LearningObject):
    """
    The common parts for all exercises.
    """
    # Timing enumeration is only used as a return value.
    TIMING = Enum([
        ('CLOSED_BEFORE', 0, "Submissions are not yet accepted"),
        ('OPEN', 1, "Normal submissions are accepted"),
        ('LATE', 2, "Late submissions are accepted"),
        ('UNOFFICIAL', 3, "Only unofficial submissions are accepted"),
        ('CLOSED_AFTER', 4, "Submissions are not anymore accepted"),
        ('ARCHIVED', 5, "Course is archived and so are exercises"),
    ])
    allow_assistant_viewing = models.BooleanField(default=True)
    allow_assistant_grading = models.BooleanField(default=False)
    min_group_size = models.PositiveIntegerField(default=1)
    max_group_size = models.PositiveIntegerField(default=1)
    max_submissions = models.PositiveIntegerField(default=10)
    max_points = models.PositiveIntegerField(default=100)
    points_to_pass = models.PositiveIntegerField(default=40)
    difficulty = models.CharField(max_length=32, blank=True)

    class Meta:
        app_label = 'exercise'

    def clean(self):
        """
        Validates the model before saving (standard method used in Django admin).
        """
        super().clean()
        errors = {}
        if self.points_to_pass > self.max_points:
            errors['points_to_pass'] = _("Points to pass cannot be greater than max_points.")
        if self.min_group_size > self.max_group_size:
            errors['min_group_size'] = _("Minimum group size cannot exceed maximum size.")
        if errors:
            raise ValidationError(errors)

    @property
    def is_submittable(self):
        return True

    def get_timing(self, students, when):
        module = self.course_module
        if not module.is_after_open(when=when):
            return self.TIMING.CLOSED_BEFORE, module.opening_time

        category = self.category
        if module.is_open(when=when) or category.confirm_the_level:
            return self.TIMING.OPEN, module.closing_time

        dl = module.course_instance.archive_time
        if dl and module.course_instance.is_archived(when=when):
            return self.TIMING.ARCHIVED, dl

        deviation = self.one_has_deadline_deviation(students)
        dl = deviation.get_new_deadline() if deviation else None
        if dl and when <= dl:
            if deviation.without_late_penalty:
                return self.TIMING.OPEN, dl
            return self.TIMING.LATE, dl

        if module.is_late_submission_open(when=when):
            return self.TIMING.LATE, module.late_submission_deadline

        dl = dl or (module.late_submission_deadline
            if module.late_submissions_allowed else module.closing_time)
        if category.accept_unofficial_submits:
            return self.TIMING.UNOFFICIAL, dl

        return self.TIMING.CLOSED_AFTER, dl

    def one_has_access(self, students, when=None):
        """
        Checks if any of the users can submit taking the granted extra time
        in consideration.
        """
        timing,d = self.get_timing(students, when or timezone.now())
        if timing == self.TIMING.OPEN:
            return True,[]
        if timing == self.TIMING.LATE:
            # xgettext:no-python-format
            return True,[_("Deadline for the exercise has passed. Late submissions are allowed until {date} but points are only worth {percent:d}% of normal.").format(
                date=date_format(d),
                percent=self.course_module.get_late_submission_point_worth(),
            )]
        if timing == self.TIMING.UNOFFICIAL:
            return True,[_("Deadline for the exercise has passed ({date}). You may still submit unofficially to receive feedback.").format(
                date=date_format(d)
            )]
        if timing == self.TIMING.CLOSED_BEFORE:
            return False,[_("The exercise opens {date} for submissions.").format(
                date=date_format(d)
            )]
        if timing == self.TIMING.CLOSED_AFTER:
            return False,[_("Deadline for the exercise has passed ({date}).").format(
                date=date_format(d)
            )]
        if timing == self.TIMING.ARCHIVED:
            return False,[_("This course has been archived ({date}).").format(
                date=date_format(d)
            )]
        return False,["ERROR"]

    def one_has_deadline_deviation(self, students):
        deviation = None
        for profile in students:
            for d in self.deadlineruledeviation_set.filter(submitter=profile):
                if not deviation\
                        or d.get_new_deadline() > deviation.get_new_deadline():
                    deviation = d
        return deviation

    def number_of_submitters(self):
        return self.course_instance.students\
            .filter(submissions__exercise=self).distinct().count()

    def get_submissions_for_student(self, user_profile, exclude_errors=False):
        if exclude_errors:
            submissions = user_profile.submissions.exclude_errors()
        else:
            submissions = user_profile.submissions
        return submissions.filter(exercise=self)

    def max_submissions_for_student(self, user_profile):
        """
        Calculates student specific max_submissions considering the possible
        MaxSubmissionsRuleDeviation for this student.
        """
        deviation = self.maxsubmissionsruledeviation_set \
            .filter(submitter=user_profile).first()
        if deviation:
            return self.max_submissions + deviation.extra_submissions
        return self.max_submissions

    def one_has_submissions(self, students):
        if self.max_submissions == 0:
            return True
        for profile in students:
            if self.get_submissions_for_student(profile, True).count() \
                    < self.max_submissions_for_student(profile):
                return True
        return False

    def no_submissions_left(self, students):
        if self.max_submissions == 0:
            return False
        for profile in students:
            if self.get_submissions_for_student(profile, True).count() \
                    <= self.max_submissions_for_student(profile):
                return False
        return True

    def is_submission_allowed(self, profile, request=None):
        """
        Checks whether the submission to this exercise is allowed for the given
        user and generates a list of warnings.

        @return: (success_flag, warning_message_list)
        """
        success, warnings, students = self._check_submission_allowed(profile, request)
        return success, list(str(w) for w in warnings), students

    def _check_submission_allowed(self, profile, request=None):
        students = [profile]
        warnings = []

        # Let course module settings decide submissionable state.
        #if self.course_instance.ending_time < timezone.now():
        #    warnings.append(_('The course is archived. Exercises are offline.'))
        #    return False, warnings, students

        # Check enrollment requirements.
        enrollment = self.course_instance.get_enrollment_for(profile.user)
        if self.status in (
            LearningObject.STATUS.ENROLLMENT,
            LearningObject.STATUS.ENROLLMENT_EXTERNAL,
        ):
            if not self.course_instance.is_enrollable(profile.user):
                return False, [_('You cannot enroll in the course.')], students
        elif not enrollment:
            # TODO Provide button to enroll, should there be option to auto-enroll
            return self.course_instance.is_course_staff(profile.user), [_('You must enroll at course home to submit exercises.')], students

        # Support group id from post or currently selected group.
        group = None
        group_id = request.POST.get("_aplus_group") if request else None
        if not group_id is None:
            try:
                gid = int(group_id)
                if gid > 0:
                    group = profile.groups.filter(
                        course_instance=self.course_instance,
                        id=gid
                    ).first()
            except ValueError:
                pass
        elif enrollment and enrollment.selected_group:
            group = enrollment.selected_group

        # Check groups cannot be changed after submitting.
        submissions = list(self.get_submissions_for_student(profile))
        if len(submissions) > 0:
            s = submissions[0]
            if self._detect_group_changes(profile, group, s):
                msg = str(_("Group can only change between different exercises."))
                if s.submitters.count() == 1:
                    warnings.append(_("You have previously submitted to this exercise alone.") + " " + msg)
                else:
                    warnings.append(_("You have previously submitted to this exercise with {collaborators}.").format(
                        collaborators=StudentGroup.format_collaborator_names(s.submitters.all(), profile)
                    ) + " " + msg)
                return False, warnings, students
        elif self._detect_submissions(profile, group):
            warnings.append(_('{collaborators} already submitted to this exercise in a different group.').format(
                collaborators=group.collaborator_names(profile)
            ))
            return False, warnings, students

        # Get submitters.
        if group:
            students = list(group.members.all())

        # Check group size.
        if not (self.min_group_size <= len(students) <= self.max_group_size):
            if self.max_group_size == self.min_group_size:
                size = "{:d}".format(self.min_group_size)
            else:
                size = "{:d}-{:d}".format(self.min_group_size, self.max_group_size)
            warnings.append(_("This exercise must be submitted in groups of {size} students.").format(
                size=size
            ))

        access_ok,access_warnings = self.one_has_access(students)

        if not self.one_has_submissions(students):
            if self.course_module.late_submissions_allowed:
                access_warnings.append(_('You have used the allowed amount of submissions for this exercise. You may still submit unofficially to receive feedback.'))
            else:
                warnings.append(_('You have used the allowed amount of submissions for this exercise.'))

        ok = (
            (access_ok and len(warnings) == 0) or
            all(self.course_instance.is_course_staff(p.user) for p in students)
        )
        return ok, warnings + access_warnings, students

    def _detect_group_changes(self, profile, group, submission):
        submitters = list(submission.submitters.all())
        if group:
            return not group.equals(submitters)
        else:
            return len(submitters) > 1 or submitters[0] != profile

    def _detect_submissions(self, profile, group):
        if group:
            return not all((
                len(self.get_submissions_for_student(p)) == 0
                for p in group.members.all() if p != profile
            ))
        return False

    def get_total_submitter_count(self):
        return UserProfile.objects \
            .filter(submissions__exercise=self) \
            .distinct().count()

    def get_load_url(self, language, request, students, url_name="exercise"):
        if self.id:
            if request.user.is_authenticated():
                user = request.user
                submission_count = self.get_submissions_for_student(user.userprofile).count()
            else:
                user = None
                submission_count = 0
            # Make grader async URL for the currently authenticated user.
            # The async handler will handle group selection at submission time.
            submission_url = update_url_params(
                api_reverse("exercise-grader", kwargs={
                    'exercise_id': self.id
                }),
                get_graderauth_exercise_params(self, user),
            )
            return self._build_service_url(
                language, request, students,
                submission_count + 1, url_name, submission_url
            )
        return super().get_load_url(language, request, students, url_name)

    def grade(self, request, submission, no_penalties=False, url_name="exercise"):
        """
        Loads the exercise feedback page.
        """
        language = get_language()
        submission_url = update_url_params(
            api_reverse("submission-grader", kwargs={
                'submission_id': submission.id
            }),
            get_graderauth_submission_params(submission),
        )
        url = self._build_service_url(
            language, request, submission.submitters.all(),
            submission.ordinal_number(), url_name, submission_url
        )
        return load_feedback_page(
            request, url, self, submission, no_penalties=no_penalties
        )

    def modify_post_parameters(self, data, files, user, students, host, url):
        """
        Allows to modify submission POST parameters before they are sent to
        the grader. Extending classes may implement this function.
        """
        pass

    def _build_service_url(self, language, request, students, ordinal_number, url_name, submission_url):
        """
        Generates complete URL with added parameters to the exercise service.
        """
        uid_str = '-'.join(sorted(str(profile.user.id) for profile in students)) if students else ''
        auri = (
            settings.OVERRIDE_SUBMISSION_HOST + submission_url
            if settings.OVERRIDE_SUBMISSION_HOST
            else request.build_absolute_uri(submission_url)
        )
        return update_url_params(self.service_url, {
            "max_points": self.max_points,
            "max_submissions": self.max_submissions,
            "submission_url": auri,
            "post_url": request.build_absolute_uri(str(self.get_url(url_name))),
            "uid": uid_str,
            "ordinal_number": ordinal_number,
            "lang": language,
        })


class LTIExercise(BaseExercise):
    """
    Exercise launched by LTI or optionally amending A+ protocol with LTI data.
    """
    lti_service = models.ForeignKey(LTIService)
    context_id = models.CharField(max_length=128, blank=True,
        help_text=_('Default: [hostname]/[course:url]/[instance:url]/'))
    resource_link_id = models.CharField(max_length=128, blank=True,
        help_text=_('Default: [aplusexercise:id]'))
    resource_link_title = models.CharField(max_length=128, blank=True,
        help_text=_('Default: Launch exercise'))
    aplus_get_and_post = models.BooleanField(default=False,
        help_text=_('Perform GET and POST from A+ to custom service URL with LTI data appended.'))

    def clean(self):
        """
        Validates the model before saving (standard method used in Django admin).
        """
        super().clean()
        if self.service_url and not has_same_domain(self.service_url, self.lti_service.url):
            raise ValidationError({
                'service_url':_("Exercise must be located in the LTI domain.")
            })

    def load(self, request, students, url_name="exercise"):
        if self.aplus_get_and_post:
            return super().load(request, students, url_name=url_name)

        if not students:
            return ExercisePage(self)

        url = self.service_url or self.lti_service.url
        lti = self._get_lti(students[0].user, students, request.get_host())

        # Render launch button.
        page = ExercisePage(self)
        page.content = self.content
        template = loader.get_template('exercise/model/_lti_button.html')
        page.content += template.render(Context({
            'service': self.lti_service,
            'url': url,
            'parameters': lti.sign_post_parameters(url),
            'title': self.resource_link_title,
        }))
        return page

    def _get_lti(self, user, students, host, add=None):
        return CustomStudentInfoLTIRequest(
            self.lti_service,
            user,
            students,
            self.course_instance,
            host,
            self.resource_link_title or self.name,
            self.context_id or None,
            self.resource_link_id or "aplusexercise{:d}".format(self.id or 0),
            add,
        )

    def get_load_url(self, language, request, students, url_name="exercise"):
        url = super().get_load_url(language, request, students, url_name)
        if self.lti_service and students:
            lti = self._get_lti(students[0].user, [], request.get_host())
            return lti.sign_get_query(url)
        return url

    def modify_post_parameters(self, data, files, user, students, host, url):
        literals = {key: str(val[0]) for key,val in data.items()}
        lti = self._get_lti(user, students, host, add=literals)
        data.update(lti.sign_post_parameters(url))


class StaticExercise(BaseExercise):
    """
    Static exercises are used for storing submissions on the server, but not automatically
    assessing them. Static exercises may be retrieved by other services through the API.

    Chapters should be used for non submittable content.

    Should be deprecated as a contradiction to A+ ideology.
    """
    exercise_page_content = models.TextField()
    submission_page_content = models.TextField()

    def load(self, request, students, url_name="exercise"):
        page = ExercisePage(self)
        page.content = self.exercise_page_content
        return page

    def grade(self, request, submission, no_penalties=False, url_name="exercise"):
        page = ExercisePage(self)
        page.content = self.submission_page_content
        page.is_accepted = True
        return page

    def _is_empty(self):
        return not bool(self.exercise_page_content)


def build_upload_dir(instance, filename):
    """
    Returns the path to a directory where the attachment file should be saved.
    This is called every time a new ExerciseWithAttachment model is created.

    @param instance: the ExerciseWithAttachment object
    @param filename: the actual name of the submitted file
    @return: a path where the file should be stored, relative to MEDIA_ROOT directory
    """
    return "course_instance_{:d}/exercise_attachment_{:d}/{}".format(
        instance.course_instance.id,
        instance.id,
        safe_file_name(filename)
    )


class ExerciseWithAttachment(BaseExercise):
    """
    ExerciseWithAttachment is an exercise type where the exercise instructions
    are stored locally and the exercise will be graded by sending an additional
    attachment to the grader together with other POST data. The exercise page
    will contain a submission form for the files the user should submit if the
    files to be submitted are defined. Otherwise the instructions must contain
    the submission form.

    Could be deprecated as a contradiction to A+ purist ideology.
    """
    files_to_submit = models.CharField(max_length=200, blank=True,
        help_text=_("File names that user should submit, use pipe character to separate files"))
    attachment = models.FileField(upload_to=build_upload_dir)

    class Meta:
        verbose_name_plural = "exercises with attachment"

    def get_files_to_submit(self):
        """
        Returns a list of the file names that user should submit with this exercise.
        """
        if len(self.files_to_submit.strip()) == 0:
            return []
        else:
            files = self.files_to_submit.split("|")
            return [filename.strip() for filename in files]

    def load(self, request, students, url_name="exercise"):
        page = ExercisePage(self)
        page.content = self.content

        # Adds the submission form to the content if there are files to be
        # submitted. A template is used to avoid hard-coded HTML here.
        if self.get_files_to_submit():
            template = loader.get_template('exercise/model/_file_submit_form.html')
            context = Context({'files' : self.get_files_to_submit()})
            page.content += template.render(context)

        return page

    def modify_post_parameters(self, data, files, user, students, host, url):
        """
        Adds the attachment file to post parameters.
        """
        import os
        files['content_0'] = (
            os.path.basename(self.attachment.path),
            open(self.attachment.path, "rb")
        )


def _delete_file(sender, instance, **kwargs):
    """
    Deletes exercise attachment file after the exercise in database is removed.
    """
    default_storage.delete(instance.attachment.path)


def _clear_cache(sender, instance, **kwargs):
    """
    Clears parent's cached html if any.
    """
    if instance.parent:
        ExerciseCache.invalidate(instance.parent)


post_delete.connect(_delete_file, ExerciseWithAttachment)
post_save.connect(_clear_cache, LearningObject)
