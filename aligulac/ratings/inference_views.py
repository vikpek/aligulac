# {{{ Imports
from datetime import date
from dateutil.relativedelta import relativedelta

from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import (
    redirect, 
    render_to_response,
)

from aligulac.cache import cache_page
from aligulac.tools import (
    base_ctx,
    get_param_range,
    Message,
    NotUniquePlayerMessage,
    StrippedCharField,
)

from ratings.models import (
    Match,
    Player,
)
from ratings.tools import (
    count_matchup_player,
    count_winloss_games,
    count_winloss_player,
    find_player,
)

from simul.playerlist import make_player
from simul.formats.match import Match as MatchSim
# }}}

# {{{ Prediction formats
FORMATS = [{
    'url':        'match',
    'name':       'Best-of-N match',
    'np-check':   lambda a: a == 2,
    'np-errmsg':  "Expected exactly two players.",
    'bo-check':   lambda a: a == 1,
    'bo-errmsg':  "Expected exactly one 'best of'.",
}, {
    'url':        'dual',
    'name':       'Dual tournament',
    'np-check':   lambda a: a == 4,
    'np-errmsg':  "Expected exactly four players.",
    'bo-check':   lambda a: a == 1,
    'bo-errmsg':  "Expected exactly one 'best of'.",
}, {
    'url':        'sebracket',
    'name':       'Single elimination bracket',
    'np-check':   lambda a: a > 1 and not a & (a-1), 
    'np-errmsg':  "Expected number of players to be a power of two (2,4,8,…).",
    'bo-check':   lambda a: a > 0,
    'bo-errmsg':  "Expected at least one 'best of'.",
}, {
    'url':        'rrgroup',
    'name':       'Round robin group',
    'np-check':   lambda a: a > 2,
    'np-errmsg':  "Expected at least three players.",
    'bo-check':   lambda a: a == 1,
    'bo-errmsg':  "Expected exactly one 'best of'.",

}, {
    'url':        'proleague',
    'name':       'Proleague team match',
    'np-check':   lambda a: a % 2 == 0,
    'np-errmsg':  "Expected an even number of players.",
    'bo-check':   lambda a: a == 1,
    'bo-errmsg':  "Expected exactly one 'best of'.",
}]
# }}}

# {{{ PredictForm: Form for entering a prediction request.
class PredictForm(forms.Form):
    format = forms.ChoiceField(
        choices=[(i, f['name']) for i, f in enumerate(FORMATS)],
        required=True,
        label='Format',
        initial=0,
    )
    bestof = StrippedCharField(max_length=100, required=True, label='Best of', initial='1')
    players = forms.CharField(max_length=10000, required=True, label='Players', initial='')

    # {{{ Constructor
    def __init__(self, request=None):
        if request is not None:
            super(PredictForm, self).__init__(request.GET)
        else:
            super(PredictForm, self).__init__()

        self.label_suffix = ''
    # }}}

    # {{{ Custom validation of bestof and players
    def clean_bestof(self):
        try:
            ret = []
            for a in map(int, self.cleaned_data['bestof'].split(',')):
                ret.append(a)
                assert(a > 0 and a % 2 == 1)
            return ret
        except:
            raise ValidationError("Must be a comma-separated list of positive odd integers (1,3,5,…).")

    def clean_players(self):
        lines = self.cleaned_data['players'].splitlines()
        lineno, ok, players = -1, True, []
        self.messages = []

        for line in lines:
            lineno += 1
            if line.strip() == '':
                continue

            pls = find_player(query=line, make=False)
            if not pls.exists():
                self.messages.append(Message("No matches found: '%s'." % line.strip(), type=Message.ERROR))
                ok = False
            elif pls.count() > 1:
                self.messages.append(NotUniquePlayerMessage(
                    line.strip(), pls, update=self['players'].auto_id,
                    updateline=lineno, type=Message.ERROR
                ))
                ok = False
            else:
                players.append(pls.first())

        if not ok:
            raise ValidationError('One or more errors found in player list.')

        return players
    # }}}

    # {{{ Combined validation of format, bestof and players
    def clean(self):
        self.cleaned_data['format'] = min(max(int(self.cleaned_data['format']), 0), len(FORMATS))
        fmt = FORMATS[self.cleaned_data['format']]

        if 'players' in self.cleaned_data and not fmt['np-check'](len(self.cleaned_data['players'])):
            raise ValidationError(fmt['np-errmsg'])
        if 'bestof' in self.cleaned_data and not fmt['bo-check'](len(self.cleaned_data['bestof'])):
            raise ValidationError(fmt['bo-errmsg'])

        return self.cleaned_data
    # }}}

    # {{{ get_messages: Returns a list of messages after validation
    def get_messages(self):
        if not self.is_valid():
            ret = []
            for field, errors in self.errors.items():
                for error in errors:
                    if field == '__all__':
                        ret.append(Message(error, type=Message.ERROR))
                    else:
                        ret.append(Message(error=error, field=self.fields[field].label))
            return self.messages + ret

        return self.messages
    # }}}

    # {{{ generate_url: Returns an URL to continue to (assumes validation has passed)
    def generate_url(self):
        return '/inference/%s/?bo=%s&ps=%s' % (
            FORMATS[self.cleaned_data['format']]['url'],
            '%2C'.join([str(b) for b in self.cleaned_data['bestof']]),
            '%2C'.join([str(p.id) if p is not None else '0' for p in self.cleaned_data['players']]),
        )
    # }}}
# }}}

# {{{ SetupForm: Form for getting the bo and player data from GET for each prediction format.
class SetupForm(forms.Form):
    bo = forms.CharField(max_length=200, required=True)
    ps = forms.CharField(max_length=200, required=True)

    # {{{ Cleaning methods. NO VALIDATION IS PERFORMED HERE.
    def clean_bo(self):
        return [(int(a)+1)//2 for a in self.cleaned_data['bo'].split(',')]

    def clean_ps(self):
        ids = [int(a) for a in self.cleaned_data['ps'].split(',')]
        players = Player.objects.in_bulk(ids)
        return [players[id] if id in players else None for id in ids]
    # }}}
# }}}

# {{{ predict view
@cache_page
def predict(request):
    base = base_ctx('Inference', 'Predict', request=request)

    if 'submitted' not in request.GET:
        base['form'] = PredictForm()
        return render_to_response('predict.html', base)

    base['form'] = PredictForm(request=request)
    base['messages'] += base['form'].get_messages()

    if not base['form'].is_valid():
        return render_to_response('predict.html', base)
    return redirect(base['form'].generate_url())
# }}}

# {{{ match prediction view
@cache_page
def match(request):
    base = base_ctx('Inference', 'Predict', request=request)

    # {{{ Get data, set up and simulate
    form = SetupForm(request.GET)
    if not form.is_valid():
        return redirect('/inference/')

    num = form.cleaned_data['bo'][0]
    dbpl = form.cleaned_data['ps']
    sipl = [make_player(p) for p in dbpl]

    match = MatchSim(num)
    match.set_players(sipl)
    match.modify(
        get_param_range(request, 's1', (0, num), 0),
        get_param_range(request, 's2', (0, num), 0),
    )
    match.compute()
    # }}}

    # {{{ Postprocessing
    base.update({
        'form': form,
        'dbpl': dbpl,
        'rta': sipl[0].elo_vs_opponent(sipl[1]),
        'rtb': sipl[1].elo_vs_opponent(sipl[0]),
        'proba': match.get_tally()[sipl[0]][1],
        'probb': match.get_tally()[sipl[1]][1],
        'match': match,
    })

    base.update({
        'max': max(base['proba'], base['probb']),
        'fav': dbpl[0] if base['proba'] > base['probb'] else dbpl[1],
    })

    resa, resb = [], []
    outcomes = [
        {'sca': outcome[1], 'scb': outcome[2], 'prob': outcome[0]} 
        for outcome in match.instances_detail()
    ]
    resa = [oc for oc in outcomes if oc['sca'] > oc['scb']]
    resb = [oc for oc in outcomes if oc['scb'] > oc['sca']]
    if len(resa) < len(resb):
        resa = [None] * (len(resb) - len(resa)) + resa
    else:
        resb = [None] * (len(resa) - len(resb)) + resb
    base['res'] = zip(resa, resb)
    # }}}

    # {{{ Scores and other data
    base['tot_w_a'], base['tot_l_a'] = count_winloss_player(dbpl[0].get_matchset(), dbpl[0])
    base['frm_w_a'], base['frm_l_a'] = count_winloss_player(
        dbpl[0].get_matchset().filter(date__gte=date.today()-relativedelta(months=4)), dbpl[0]
    )
    base['tot_w_b'], base['tot_l_b'] = count_winloss_player(dbpl[1].get_matchset(), dbpl[1])
    base['frm_w_b'], base['frm_l_b'] = count_winloss_player(
        dbpl[1].get_matchset().filter(date__gte=date.today()-relativedelta(months=4)), dbpl[1]
    )
    if dbpl[1].race in 'PTZ':
        base['mu_w_a'], base['mu_l_a'] = count_matchup_player(dbpl[0].get_matchset(), dbpl[0], dbpl[1].race)
        base['fmu_w_a'], base['fmu_l_a'] = count_matchup_player(
            dbpl[0].get_matchset().filter(date__gte=date.today()-relativedelta(months=4)),
            dbpl[0], dbpl[1].race
        )
    if dbpl[0].race in 'PTZ':
        base['mu_w_b'], base['mu_l_b'] = count_matchup_player(dbpl[1].get_matchset(), dbpl[1], dbpl[0].race)
        base['fmu_w_b'], base['fmu_l_b'] = count_matchup_player(
            dbpl[1].get_matchset().filter(date__gte=date.today()-relativedelta(months=4)), 
            dbpl[1], dbpl[0].race
        )
    wa_a, wb_a = count_winloss_games(Match.objects.filter(pla=dbpl[0], plb=dbpl[1]))
    wb_b, wa_b = count_winloss_games(Match.objects.filter(pla=dbpl[1], plb=dbpl[0]))
    base['vs_a'] = wa_a + wa_b
    base['vs_b'] = wb_a + wb_b
    # }}}

    return render_to_response('pred_match.html', base)
# }}}