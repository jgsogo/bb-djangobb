import math
from datetime import datetime, timedelta 

from django.shortcuts import get_object_or_404, render
from django.http import Http404, HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.core.cache import cache
from django.db.models import Q, F, Sum
from django.utils.encoding import smart_str
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt

from djangobb_forum.util import paged, build_form, paginate, set_language
from djangobb_forum.models import Category, Forum, Topic, Post, Profile, Reputation,\
    Attachment, PostTracking
from djangobb_forum.forms import AddPostForm, EditPostForm, UserSearchForm,\
    PostSearchForm, ReputationForm, MailToForm, EssentialsProfileForm,\
    PersonalProfileForm, MessagingProfileForm, PersonalityProfileForm,\
    DisplayProfileForm, PrivacyProfileForm, ReportForm, UploadAvatarForm
from djangobb_forum.templatetags import forum_extras
from djangobb_forum import settings as forum_settings
from djangobb_forum.util import smiles, convert_text_to_html
from djangobb_forum.templatetags.forum_extras import forum_moderated_by

from haystack.query import SearchQuerySet, SQ


def index(request, full=True):
    users_cached = cache.get('users_online', {})
    users_online = users_cached and User.objects.filter(id__in = users_cached.keys()) or []
    guests_cached = cache.get('guests_online', {})
    guest_count = len(guests_cached)
    users_count = len(users_online)

    cats = {}
    forums = {}
    user_groups = request.user.groups.all()
    if request.user.is_anonymous():  # in django 1.1 EmptyQuerySet raise exception
        user_groups = []
    _forums = Forum.objects.filter(
            Q(category__groups__in=user_groups) | \
            Q(category__groups__isnull=True)).select_related('last_post__topic',
                                                            'last_post__user',
                                                            'category')
    for forum in _forums:
        cat = cats.setdefault(forum.category.id,
            {'id': forum.category.id, 'cat': forum.category, 'forums': []})
        cat['forums'].append(forum)
        forums[forum.id] = forum

    cmpdef = lambda a, b: cmp(a['cat'].position, b['cat'].position)
    cats = sorted(cats.values(), cmpdef)

    to_return = {'cats': cats,
                'posts': Post.objects.count(),
                'topics': Topic.objects.count(),
                'users': User.objects.count(),
                'users_online': users_online,
                'online_count': users_count,
                'guest_count': guest_count,
                'last_user': User.objects.latest('date_joined'),
                }
    if full:
        return render(request, 'djangobb_forum/index.html', to_return)
    else:
        return render(request, 'djangobb_forum/lofi/index.html', to_return)


@transaction.commit_on_success
@paged('topics', forum_settings.FORUM_PAGE_SIZE)
def moderate(request, forum_id):
    forum = get_object_or_404(Forum, pk=forum_id)
    topics = forum.topics.order_by('-sticky', '-updated').select_related()
    if request.user.is_superuser or request.user in forum.moderators.all():
        topic_ids = request.POST.getlist('topic_id')
        if 'move_topics' in request.POST:
            return render(request,  'djangobb_forum/move_topic.html', {
                'categories': Category.objects.all(),
                'topic_ids': topic_ids,
                'exclude_forum': forum,
            })
        elif 'delete_topics' in request.POST:
            for topic_id in topic_ids:
                topic = get_object_or_404(Topic, pk=topic_id)
                topic.delete()
            return HttpResponseRedirect(reverse('djangobb:index'))
        elif 'open_topics' in request.POST:
            for topic_id in topic_ids:
                open_close_topic(request, topic_id, 'o')
            return HttpResponseRedirect(reverse('djangobb:index'))
        elif 'close_topics' in request.POST:
            for topic_id in topic_ids:
                open_close_topic(request, topic_id, 'c')
            return HttpResponseRedirect(reverse('djangobb:index'))

        return render(request, 'djangobb_forum/moderate.html', {'forum': forum,
                'topics': topics,
                #'sticky_topics': forum.topics.filter(sticky=True),
                'paged_qs': topics,
                'posts': forum.posts.count(),
                })
    else:
        raise Http404


@paged('results', forum_settings.SEARCH_PAGE_SIZE)
def search(request):
    # TODO: move to form
    if 'action' in request.GET:
        action = request.GET['action']
        #FIXME: show_user for anonymous raise exception, 
        #django bug http://code.djangoproject.com/changeset/14087 :|
        groups = request.user.groups.all() or [] #removed after django > 1.2.3 release
        topics = Topic.objects.filter(
                   Q(forum__category__groups__in=groups) | \
                   Q(forum__category__groups__isnull=True))
        if action == 'show_24h':
            date = datetime.today() - timedelta(1)
            topics = topics.filter(created__gte=date)
        elif action == 'show_new':
            last_read = PostTracking.objects.get(user=request.user).last_read
            if last_read:
                topics = topics.filter(last_post__updated__gte=last_read).all()
            else:
                #searching more than forum_settings.SEARCH_PAGE_SIZE in this way - not good idea :]
                topics = [topic for topic in topics[:forum_settings.SEARCH_PAGE_SIZE] if forum_extras.has_unreads(topic, request.user)]
        elif action == 'show_unanswered':
            topics = topics.filter(post_count=1)
        elif action == 'show_subscriptions':
            topics = topics.filter(subscribers__id=request.user.id)
        elif action == 'show_user':
            user_id = request.GET['user_id']
            posts = Post.objects.filter(user__id=user_id)
            topics = [post.topic for post in posts if post.topic in topics]
        elif action == 'search':
            keywords = request.GET.get('keywords')
            author = request.GET.get('author')
            forum = request.GET.get('forum')
            search_in = request.GET.get('search_in')
            sort_by = request.GET.get('sort_by')
            sort_dir = request.GET.get('sort_dir')

            if not (keywords or author):
                return HttpResponseRedirect(reverse('djangobb:search'))

            query = SearchQuerySet().models(Post)

            if author:
                query = query.filter(author__username=author)

            if forum != u'0':
                query = query.filter(forum__id=forum)

            if keywords:
                if search_in == 'all':
                    query = query.filter(SQ(topic=keywords) | SQ(text=keywords))
                elif search_in == 'message':
                    query = query.filter(text=keywords)
                elif search_in == 'topic':
                    query = query.filter(topic=keywords)

            order = {'0': 'created',
                     '1': 'author',
                     '2': 'topic',
                     '3': 'forum'}.get(sort_by, 'created')
            if sort_dir == 'DESC':
                order = '-' + order

            posts = query.order_by(order)

            if 'topics' in request.GET['show_as']:
                topics = []
                topics_to_exclude = SQ()
                for post in posts:
                    if post.object.topic not in topics:
                        if post.object.topic.forum.category.has_access(request.user):
                            topics.append(post.object.topic)
                        else:
                            topics_to_exclude |= SQ(topic=post.object.topic)

                if topics_to_exclude:
                    posts = posts.exclude(topics_to_exclude)
                return render(request, 'djangobb_forum/search_topics.html', {'paged_qs': topics})
            elif 'posts' in request.GET['show_as']:
                return render(request, 'djangobb_forum/search_posts.html', {'paged_qs': topics})
        return render(request, 'djangobb_forum/search_topics.html', {'paged_qs': topics})
    else:
        form = PostSearchForm()
        return render(request, 'djangobb_forum/search_form.html', {'categories': Category.objects.all(),
                'form': form,
                })


@login_required
def misc(request):
    if 'action' in request.GET:
        action = request.GET['action']
        if action =='markread':
            user = request.user
            PostTracking.objects.filter(user__id=user.id).update(last_read=datetime.now(), topics=None)
            return HttpResponseRedirect(reverse('djangobb:index'))

        elif action == 'report':
            if request.GET.get('post_id', ''):
                post_id = request.GET['post_id']
                post = get_object_or_404(Post, id=post_id)
                form = build_form(ReportForm, request, reported_by=request.user, post=post_id)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(post.get_absolute_url())
                return (request, 'djangobb_forum/report.html', {'form':form})

    elif 'submit' in request.POST and 'mail_to' in request.GET:
        form = MailToForm(request.POST)
        if form.is_valid():
            user = get_object_or_404(User, username=request.GET['mail_to'])
            subject = form.cleaned_data['subject']
            body = form.cleaned_data['body'] + '\n %s %s [%s]' % (Site.objects.get_current().domain,
                                                                  request.user.username,
                                                                  request.user.email)
            user.email_user(subject, body, request.user.email)
            return HttpResponseRedirect(reverse('djangobb:index'))

    elif 'mail_to' in request.GET:
        mailto = get_object_or_404(User, username=request.GET['mail_to'])
        form = MailToForm()
        return (request, 'djangobb_forum/mail_to.html', {'form':form,
                'mailto': mailto,
               })


@paged('topics', forum_settings.FORUM_PAGE_SIZE)
def show_forum(request, forum_id, full=True):
    forum = get_object_or_404(Forum, pk=forum_id)
    if not forum.category.has_access(request.user):
        return HttpResponseForbidden()
    topics = forum.topics.order_by('-sticky', '-updated').select_related()
    moderator = request.user.is_superuser or\
        request.user in forum.moderators.all()
    to_return = {'categories': Category.objects.all(),
                'forum': forum,
                'paged_qs': topics,
                'posts': forum.post_count,
                'topics': forum.topic_count,
                'moderator': moderator,
                }
    if full:
        return render(request, 'djangobb_forum/forum.html', to_return)
    else:
        pages, paginator, paged_list_name = paginate(topics, request, forum_settings.FORUM_PAGE_SIZE)
        to_return.update({'pages': pages,
                        'paginator': paginator,
                        'topics': paged_list_name,
                        })
        del to_return['paged_qs']
        return render(request, 'djangobb_forum/lofi/forum.html', to_return)


@transaction.commit_on_success
def show_topic(request, topic_id, full=True):
    topic = get_object_or_404(Topic.objects.select_related(), pk=topic_id)
    if not topic.forum.category.has_access(request.user):
        return HttpResponseForbidden()
    Topic.objects.filter(pk=topic.id).update(views=F('views') + 1)

    last_post = topic.last_post

    if request.user.is_authenticated():
        topic.update_read(request.user)
    #@paged can't be used in this view. (ticket #180)
    #TODO: must be refactored (ticket #39)
    from django.core.paginator import Paginator, EmptyPage, InvalidPage
    try:
        page = int(request.GET.get('page', 1))
    except ValueError:
        page = 1
    paginator = Paginator(topic.posts.all().select_related(), forum_settings.TOPIC_PAGE_SIZE)
    try:
        page_obj = paginator.page(page)
    except (InvalidPage, EmptyPage):
        raise Http404
    posts = page_obj.object_list
    users = set(post.user.id for post in posts)
    profiles = Profile.objects.filter(user__pk__in=users)
    profiles = dict((profile.user_id, profile) for profile in profiles)

    for post in posts:
        post.user.forum_profile = profiles[post.user.id]

    if forum_settings.REPUTATION_SUPPORT:
        replies_list = Reputation.objects.filter(to_user__pk__in=users).values('to_user_id').annotate(Sum('sign'))
        replies = {}
        for r in replies_list:
            replies[r['to_user_id']] = r['sign__sum']

        for post in posts:
            post.user.forum_profile.reply_total = replies.get(post.user.id, 0)

    initial = {}
    if request.user.is_authenticated():
        initial = {'markup': request.user.forum_profile.markup}
    form = AddPostForm(topic=topic, initial=initial)

    moderator = request.user.is_superuser or\
        request.user in topic.forum.moderators.all()
    if request.user.is_authenticated() and request.user in topic.subscribers.all():
        subscribed = True
    else:
        subscribed = False

    highlight_word = request.GET.get('hl', '')
    if full:
        return render(request, 'djangobb_forum/topic.html', {'categories': Category.objects.all(),
                'topic': topic,
                'last_post': last_post,
                'form': form,
                'moderator': moderator,
                'subscribed': subscribed,
                'posts': posts,
                'highlight_word': highlight_word,
                
                'page': page,
                'page_obj': page_obj,
                'pages': paginator.num_pages,
                'results_per_page': paginator.per_page,
                'is_paginated': page_obj.has_other_pages(),
                })
    else:
        return render(request, 'djangobb_forum/lofi/topic.html', {'categories': Category.objects.all(),
                'topic': topic,
                'pages': paginator.num_pages,
                'paginator': paginator,
                'posts': posts,
                })


@login_required
@transaction.commit_on_success
def add_post(request, forum_id, topic_id):
    forum = None
    topic = None
    posts = None

    if forum_id:
        forum = get_object_or_404(Forum, pk=forum_id)
        if not forum.category.has_access(request.user):
            return HttpResponseForbidden()
    elif topic_id:
        topic = get_object_or_404(Topic, pk=topic_id)
        posts = topic.posts.all().select_related()
        if not topic.forum.category.has_access(request.user):
            return HttpResponseForbidden()
    if topic and topic.closed:
        return HttpResponseRedirect(topic.get_absolute_url())

    ip = request.META.get('REMOTE_ADDR', None)
    form = build_form(AddPostForm, request, topic=topic, forum=forum,
                      user=request.user, ip=ip,
                      initial={'markup': request.user.forum_profile.markup})

    if 'post_id' in request.GET:
        post_id = request.GET['post_id']
        post = get_object_or_404(Post, pk=post_id)
        form.fields['body'].initial = "[quote=%s]%s[/quote]" % (post.user, post.body)

    if form.is_valid():
        post = form.save();
        return HttpResponseRedirect(post.get_absolute_url())

    return render(request, 'djangobb_forum/add_post.html', {'form': form,
            'posts': posts,
            'topic': topic,
            'forum': forum,
            })


@transaction.commit_on_success
def user(request, username):
    user = get_object_or_404(User, username=username)
    if request.user.is_authenticated() and user == request.user or request.user.is_superuser:
        if 'section' in request.GET:
            section = request.GET['section']
            profile_url = reverse('djangobb:forum_profile', args=[user.username]) + '?section=' + section  
            if section == 'privacy':
                form = build_form(PrivacyProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return render(request, 'djangobb_forum/profile/profile_privacy.html', {'active_menu':'privacy',
                        'profile': user,
                        'form': form,
                       })
            elif section == 'display':
                form = build_form(DisplayProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return render(request, 'djangobb_forum/profile/profile_display.html', {'active_menu':'display',
                        'profile': user,
                        'form': form,
                       })
            elif section == 'personality':
                form = build_form(PersonalityProfileForm, request, markup=user.forum_profile.markup, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return render(request, 'djangobb_forum/profile/profile_personality.html', {'active_menu':'personality',
                        'profile': user,
                        'form': form,
                        })
            elif section == 'messaging':
                form = build_form(MessagingProfileForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return render(request, 'djangobb_forum/profile/profile_messaging.html', {'active_menu':'messaging',
                        'profile': user,
                        'form': form,
                       })
            elif section == 'personal':
                form = build_form(PersonalProfileForm, request, instance=user.forum_profile, user=user)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(profile_url)
                return render(request, 'djangobb_forum/profile/profile_personal.html', {'active_menu':'personal',
                        'profile': user,
                        'form': form,
                       })
            elif section == 'essentials':
                form = build_form(EssentialsProfileForm, request, instance=user.forum_profile,
                                  user_view=user, user_request=request.user)
                if request.method == 'POST' and form.is_valid():
                    profile = form.save()
                    set_language(request, profile.language)
                    return HttpResponseRedirect(profile_url)

                return render(request, 'djangobb_forum/profile/profile_essentials.html', {'active_menu':'essentials',
                        'profile': user,
                        'form': form,
                        })

        elif 'action' in request.GET:
            action = request.GET['action']
            if action == 'upload_avatar':
                form = build_form(UploadAvatarForm, request, instance=user.forum_profile)
                if request.method == 'POST' and form.is_valid():
                    form.save()
                    return HttpResponseRedirect(reverse('djangobb:forum_profile', args=[user.username]))
                return render(request, 'djangobb_forum/upload_avatar.html', {'form': form,
                        'avatar_width': forum_settings.AVATAR_WIDTH,
                        'avatar_height': forum_settings.AVATAR_HEIGHT,
                       })
            elif action == 'delete_avatar':
                profile = get_object_or_404(Profile, user=request.user)
                profile.avatar = None
                profile.save()
                return HttpResponseRedirect(reverse('djangobb:forum_profile', args=[user.username]))

        else:
            form = build_form(EssentialsProfileForm, request, instance=user.forum_profile,
                                  user_view=user, user_request=request.user)
            if request.method == 'POST' and form.is_valid():
                profile = form.save()
                set_language(request, profile.language)
                return HttpResponseRedirect(reverse('djangobb:forum_profile', args=[user.username]))
            return render(request, 'djangobb_forum/profile/profile_essentials.html', {'active_menu':'essentials',
                    'profile': user,
                    'form': form,
                   })
        raise Http404
    else:
        topic_count = Topic.objects.filter(user__id=user.id).count()
        if user.forum_profile.post_count < forum_settings.POST_USER_SEARCH and not request.user.is_authenticated():
            return HttpResponseRedirect(reverse('user_signin') + '?next=%s' % request.path)
        return render(request, 'djangobb_forum/user.html', {'profile': user,
                'topic_count': topic_count,
               })


@login_required
@transaction.commit_on_success
def reputation(request, username):
    user = get_object_or_404(User, username=username)
    form = build_form(ReputationForm, request, from_user=request.user, to_user=user)

    if 'action' in request.GET:
        if request.user == user:
            return HttpResponseForbidden(u'You can not change the reputation of yourself')

        if 'post_id' in request.GET:
            post_id = request.GET['post_id']
            form.fields['post'].initial = post_id
            if request.GET['action'] == 'plus':
                form.fields['sign'].initial = 1
            elif request.GET['action'] == 'minus':
                form.fields['sign'].initial = -1
            return render(request, 'djangobb_forum/reputation_form.html', {'form': form})
        else:
            raise Http404

    elif request.method == 'POST':
        if 'del_reputation' in request.POST and request.user.is_superuser:
            reputation_list = request.POST.getlist('reputation_id')
            for reputation_id in reputation_list:
                    reputation = get_object_or_404(Reputation, pk=reputation_id)
                    reputation.delete()
            return HttpResponseRedirect(reverse('djangobb:index'))
        elif form.is_valid():
            form.save()
            post_id = request.POST['post']
            post = get_object_or_404(Post, id=post_id)
            return HttpResponseRedirect(post.get_absolute_url())
        else:
            return render(request, 'djangobb_forum/reputation_form.html', {'form': form})
    else:
        reputations = Reputation.objects.filter(to_user__id=user.id).order_by('-time').select_related()
        return render(request, 'djangobb_forum/reputation.html', {'reputations': reputations,
                'profile': user.forum_profile,
               })


def show_post(request, post_id):
    post = get_object_or_404(Post, pk=post_id)
    count = post.topic.posts.filter(created__lt=post.created).count() + 1
    page = math.ceil(count / float(forum_settings.TOPIC_PAGE_SIZE))
    url = '%s?page=%d#post-%d' % (reverse('djangobb:topic', args=[post.topic.id]), page, post.id)
    return HttpResponseRedirect(url)


@login_required
@transaction.commit_on_success
def edit_post(request, post_id):
    from djangobb_forum.templatetags.forum_extras import forum_editable_by

    post = get_object_or_404(Post, pk=post_id)
    topic = post.topic
    if not forum_editable_by(post, request.user):
        return HttpResponseRedirect(post.get_absolute_url())
    form = build_form(EditPostForm, request, topic=topic, instance=post)
    if form.is_valid():
        post = form.save(commit=False)
        post.updated_by = request.user
        post.save()
        return HttpResponseRedirect(post.get_absolute_url())

    return render(request, 'djangobb_forum/edit_post.html', {'form': form,
            'post': post,
            })


@login_required
@transaction.commit_on_success
@paged('posts', forum_settings.TOPIC_PAGE_SIZE)
def delete_posts(request, topic_id):

    topic = Topic.objects.select_related().get(pk=topic_id)

    if forum_moderated_by(topic, request.user):
        deleted = False
        post_list = request.POST.getlist('post')
        for post_id in post_list:
            if not deleted:
                deleted = True
            delete_post(request, post_id)
        if deleted:
            return HttpResponseRedirect(topic.get_absolute_url())

    last_post = topic.posts.latest()

    if request.user.is_authenticated():
        topic.update_read(request.user)

    posts = topic.posts.all().select_related()

    profiles = Profile.objects.filter(user__pk__in=set(x.user.id for x in posts))
    profiles = dict((x.user_id, x) for x in profiles)

    for post in posts:
        post.user.forum_profile = profiles[post.user.id]

    initial = {}
    if request.user.is_authenticated():
        initial = {'markup': request.user.forum_profile.markup}
    form = AddPostForm(topic=topic, initial=initial)

    moderator = request.user.is_superuser or\
        request.user in topic.forum.moderators.all()
    if request.user.is_authenticated() and request.user in topic.subscribers.all():
        subscribed = True
    else:
        subscribed = False
    return render(request, 'djangobb_forum/delete_posts.html', {
            'topic': topic,
            'last_post': last_post,
            'form': form,
            'moderator': moderator,
            'subscribed': subscribed,
            'paged_qs': posts,
            })


@login_required
@transaction.commit_on_success
def move_topic(request):
    if 'topic_id' in request.GET:
        #if move only 1 topic
        topic_ids = [request.GET['topic_id']]
    else:
        topic_ids = request.POST.getlist('topic_id')
    first_topic = topic_ids[0]
    topic = get_object_or_404(Topic, pk=first_topic)
    from_forum = topic.forum
    if 'to_forum' in request.POST:
        to_forum_id = int(request.POST['to_forum'])
        to_forum = get_object_or_404(Forum, pk=to_forum_id)
        for topic_id in topic_ids:
            topic = get_object_or_404(Topic, pk=topic_id)
            if topic.forum != to_forum:
                if forum_moderated_by(topic, request.user):
                    topic.forum = to_forum
                    topic.save()

        #TODO: not DRY
        try:
            last_post = Post.objects.filter(topic__forum__id=from_forum.id).latest()
        except Post.DoesNotExist:
            last_post = None
        from_forum.last_post = last_post
        from_forum.topic_count = from_forum.topics.count()
        from_forum.post_count = from_forum.posts.count()
        from_forum.save()
        return HttpResponseRedirect(to_forum.get_absolute_url())

    return render(request, 'djangobb_forum/move_topic.html', {'categories': Category.objects.all(),
            'topic_ids': topic_ids,
            'exclude_forum': from_forum,
            })


@login_required
@transaction.commit_on_success
def stick_unstick_topic(request, topic_id, action):

    topic = get_object_or_404(Topic, pk=topic_id)
    if forum_moderated_by(topic, request.user):
        if action == 's':
            topic.sticky = True
        elif action == 'u':
            topic.sticky = False
        topic.save()
    return HttpResponseRedirect(topic.get_absolute_url())


@login_required
@transaction.commit_on_success
def delete_post(request, post_id):
    post = get_object_or_404(Post, pk=post_id)
    last_post = post.topic.last_post
    topic = post.topic
    forum = post.topic.forum

    allowed = False
    if request.user.is_superuser or\
        request.user in post.topic.forum.moderators.all() or \
        (post.user == request.user and post == last_post):
        allowed = True

    if not allowed:
        return HttpResponseRedirect(post.get_absolute_url())

    post.delete()

    try:
        Topic.objects.get(pk=topic.id)
    except Topic.DoesNotExist:
        #removed latest post in topic
        return HttpResponseRedirect(forum.get_absolute_url())
    else:
        return HttpResponseRedirect(topic.get_absolute_url())


@login_required
@transaction.commit_on_success
def open_close_topic(request, topic_id, action):

    topic = get_object_or_404(Topic, pk=topic_id)
    if forum_moderated_by(topic, request.user):
        if action == 'c':
            topic.closed = True
        elif action == 'o':
            topic.closed = False
        topic.save()
    return HttpResponseRedirect(topic.get_absolute_url())


@paged('users', forum_settings.USERS_PAGE_SIZE)
def users(request):
    users = User.objects.filter(forum_profile__post_count__gte=forum_settings.POST_USER_SEARCH).order_by('username')
    form = UserSearchForm(request.GET)
    users = form.filter(users)
    return render(request, 'djangobb_forum/users.html', {'paged_qs': users,
            'form': form,
            })


@login_required
@transaction.commit_on_success
def delete_subscription(request, topic_id):
    topic = get_object_or_404(Topic, pk=topic_id)
    topic.subscribers.remove(request.user)
    if 'from_topic' in request.GET:
        return HttpResponseRedirect(reverse('djangobb:topic', args=[topic.id]))
    else:
        return HttpResponseRedirect(reverse('djangobb:forum_profile', args=[request.user.username]))


@login_required
@transaction.commit_on_success
def add_subscription(request, topic_id):
    topic = get_object_or_404(Topic, pk=topic_id)
    topic.subscribers.add(request.user)
    return HttpResponseRedirect(reverse('djangobb:topic', args=[topic.id]))


@login_required
def show_attachment(request, hash):
    attachment = get_object_or_404(Attachment, hash=hash)
    file_data = file(attachment.get_absolute_path(), 'rb').read()
    response = HttpResponse(file_data, mimetype=attachment.content_type)
    response['Content-Disposition'] = 'attachment; filename="%s"' % smart_str(attachment.name)
    return response


@login_required
@csrf_exempt
def post_preview(request):
    '''Preview for markitup'''
    markup = request.user.forum_profile.markup
    data = request.POST.get('data', '')

    data = convert_text_to_html(data, markup)
    if forum_settings.SMILES_SUPPORT:
        data = smiles(data)
    return render(request, 'djangobb_forum/post_preview.html', {'data': data})
