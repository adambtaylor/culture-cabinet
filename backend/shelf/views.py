from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.http import Http404, HttpResponse
from dataclasses import asdict
from functools import wraps
from shelf.recovery import access_barrier
from shelf.feedback import FeedbackError, feedback_context, mutate
from shelf.forms import FeedbackForm, ExportForm
from shelf.portability import human_export
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf.authority import SELECTION_KEY, permitted_profiles, require_profile
from shelf.catalogue import CARDS



def guarded(view):
    @wraps(view)
    def call(*args, **kwargs):
        try:
            with access_barrier():
                return view(*args, **kwargs)
        except FeedbackError as error:
            return HttpResponse(str(error) + '\n', status=error.status, content_type='text/plain')
    return call


class DemoLoginView(LoginView):
    template_name = "registration/login.html"
    http_method_names = ["get", "post", "head", "options"]

    def get_success_url(self):
        return "/"


class DemoLogoutView(LoginRequiredMixin, LogoutView):
    next_page = "/login/"
    http_method_names = ["post", "options"]


@login_required
@require_GET
@guarded
def profiles(request):
    return render(request, "cabinet/profiles.html", {
        "profile_options": permitted_profiles(request.user, "select"),
    })


@login_required
@require_POST
@guarded
def select_profile(request):
    if len(request.POST.getlist("profile_id")) != 1:
        raise Http404
    profile = require_profile(request.user, request.POST["profile_id"], "select")
    request.session[SELECTION_KEY] = profile.pk
    return redirect("catalogue")


@login_required
@require_GET
@guarded
def catalogue(request):
    target = request.session.get(SELECTION_KEY)
    if target is None:
        return redirect("profiles")
    profile = require_profile(request.user, target, "view")
    return render(request, "cabinet/catalogue.html", {
        "selected_profile": profile, "cards": CARDS,
        "feedback_cards": catalogue_feedback(request.user, profile),
    })


@login_required
@require_GET
@guarded
def profile_catalogue(request, profile_id):
    profile = require_profile(request.user, profile_id, "view")
    return render(request, "cabinet/catalogue.html", {
        "selected_profile": profile, "cards": CARDS,
        "feedback_cards": catalogue_feedback(request.user, profile),
    })


def catalogue_feedback(actor, profile):
    from shelf.catalogue import TARGET_IDS
    from django.urls import reverse
    from shelf.models import FeedbackProjection
    output=[]
    for card,target_id in zip(CARDS,TARGET_IDS):
        rows=list(FeedbackProjection.objects.filter(profile=profile,target_id=target_id))
        if any(p.signal=='visibility' and p.value=={'value':'hidden'} for p in rows): continue
        output.append({'card':card,'target_id':target_id,
            'feedback_url':reverse('feedback_detail',args=[profile.pk,target_id]),
            'summary': [{'signal':p.signal,'value':p.value} for p in rows], 'is_hidden':False})
    return tuple(output)


@login_required
@require_GET
@guarded
def feedback_detail(request,profile_id,target_id):
    context=feedback_context(actor=request.user,profile_id=profile_id,target_id=target_id,include_hidden=True)
    return render(request,'cabinet/feedback.html',vars(context))


@login_required
@require_POST
@guarded
def feedback_mutate(request,profile_id,target_id):
    require_profile(request.user,profile_id,'mutate')
    if int(request.META.get('CONTENT_LENGTH') or 0)>16*1024:
        return HttpResponse('Invalid feedback.\n',status=400)
    form=FeedbackForm(request.POST,target_id=target_id)
    if form.is_valid():
        try:
            mutate(actor=request.user,profile_id=profile_id,command=form.command)
        except FeedbackError as error:
            form.add_error(None,str(error)); status=error.status
        else:
            from django.urls import reverse
            response=HttpResponse(status=303)
            response['Location']=reverse('feedback_detail',args=[profile_id,target_id])
            return response
    else: status=400
    context=vars(feedback_context(actor=request.user,profile_id=profile_id,target_id=target_id,include_hidden=True)).copy()
    context['forms']=(form,); context['message']='Review the feedback form.'
    return render(request,'cabinet/feedback.html',context,status=status)


@login_required
@guarded
def feedback_export(request,profile_id):
    if request.method not in ('GET','POST'):
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(['GET','POST'])
    profile=require_profile(request.user,profile_id,'export')
    form=ExportForm(request.POST if request.method=='POST' else None)
    if request.method=='POST' and form.is_valid():
        artifact=human_export(actor=request.user,profile_id=profile_id,format=form.cleaned_data['format'])
        require_profile(request.user,profile_id,'export')
        response=HttpResponse(artifact.content,content_type=artifact.media_type)
        response['Content-Disposition']='attachment; filename="'+artifact.filename+'"'
        return response
    return render(request,'cabinet/feedback_export.html',{'selected_profile':profile,'form':form},
        status=400 if request.method=='POST' else 200)
