"""User edits must take precedence over descriptive LoRA training tags."""
from __future__ import annotations

import unittest

from desktop_runtime.lora_profiles import resolve_selected_lora_triggers
from winefox_core.lora_service import LoraService
from winefox_core.lora_prompt import filter_reference_tags, profile_prompt_groups


WORDS = ['feranmut proxy (arknights)', 'bare shoulders', 'blue eyes',
         'cleavage cutout', 'clothing cutout', 'colored extremities',
         'detached sleeves', 'dragon girl', 'dragon horns', 'dress',
         'earrings', 'green hair', 'jewelry', 'pointy ears',
         'sleeveless dress', 'small breasts', 'white dress', 'wide sleeves']


class LoRAPromptPrecedenceTests(unittest.TestCase):
    def service(self, words=None, **extra):
        return LoraService({'illustrious':{'祀':{'character':'祀','lora':'role.safetensors','trigger_words': WORDS if words is None else words,**extra}}})

    def resolve(self, raw='', translated='', words=None, **extra):
        return resolve_selected_lora_triggers(self.service(words,**extra),'illustrious',[{'name':'role.safetensors'}],prompt=raw,translated_prompt=translated)

    def test_screenshot_swimsuit_request_keeps_identity_without_old_dress(self):
        tags=self.resolve('衣服换成死库水，展示全身，害羞的样子','full body, shy, swimsuit, school swimsuit')
        self.assertIn('feranmut proxy (arknights)',tags)
        self.assertIn('green hair',tags);self.assertIn('dragon horns',tags)
        for tag in ('dress','white dress','sleeveless dress','detached sleeves','wide sleeves','cleavage cutout','clothing cutout','bare shoulders'):
            self.assertNotIn(tag,tags)

    def test_default_outfit_requires_explicit_request(self):
        self.assertNotIn('white dress',self.resolve('在沙滩上吃冰棍','beach, eating popsicle'))
        self.assertIn('white dress',self.resolve('穿原来的衣服','original outfit'))

    def test_replacing_original_outfit_does_not_count_as_requesting_it(self):
        tags=self.resolve('不要原服，换成西装','suit, pants')
        self.assertNotIn('white dress',tags)

    def test_hair_color_override_does_not_discard_identity_or_length(self):
        tags=self.resolve('头发改成粉色','pink hair',words=['identity_token','green hair','long hair','blue eyes'])
        self.assertNotIn('green hair',tags);self.assertIn('long hair',tags)
        self.assertIn('identity_token',tags);self.assertIn('blue eyes',tags)

    def test_eye_color_override_does_not_override_hair(self):
        tags=self.resolve('眼睛改成红色','red eyes')
        self.assertNotIn('blue eyes',tags);self.assertIn('green hair',tags)

    def test_hair_length_and_expression_changes(self):
        tags=self.resolve('短发，害羞，站立全身','short hair, shy, standing, full body',words=['token','long hair','green hair','smile','sitting','upper body'])
        for old in ('long hair','smile','sitting','upper body'):self.assertNotIn(old,tags)
        self.assertIn('green hair',tags);self.assertIn('token',tags)

    def test_remove_horns_without_removing_other_identity(self):
        tags=self.resolve('去掉龙角，不要耳环','no horns, no earrings')
        self.assertNotIn('dragon horns',tags);self.assertNotIn('earrings',tags)
        self.assertIn('feranmut proxy (arknights)',tags)

    def test_weights_underscores_and_custom_activation_tokens(self):
        tags=self.resolve('换成泳装','swimsuit',words=['special_dress_character_v2','(white_dress:1.3)','(long_hair:1.2)','(unique_token:1.1)'])
        self.assertNotIn('(white_dress:1.3)',tags)
        self.assertIn('special_dress_character_v2',tags)
        self.assertIn('(long_hair:1.2)',tags);self.assertIn('(unique_token:1.1)',tags)

    def test_empty_user_trigger_list_never_revives_stale_profile_fields(self):
        self.assertEqual(self.resolve('穿原服','original outfit',words=[],activation_tags=['stale_token'],default_outfit=['white dress']),())

    def test_color_change_keeps_hairstyle(self):
        tags = self.resolve('头发改成粉色', 'pink hair', words=['token','green hair','long hair','bangs','braid'])
        self.assertNotIn('green hair',tags)
        for tag in ('long hair','bangs','braid'): self.assertIn(tag,tags)

    def test_import_groups_preserve_author_data(self):
        words = WORDS.copy()
        groups = profile_prompt_groups(words)
        self.assertEqual(words,WORDS)
        self.assertIn('feranmut proxy (arknights)',groups['activation'])
        self.assertIn('green hair',groups['appearance'])
        self.assertIn('white dress',groups['outfit'])

    def test_vision_reference_cannot_restore_the_old_outfit(self):
        plan = filter_reference_tags(['white dress','detached sleeves','green hair','sitting'], '换成泳装，站着', 'swimsuit, standing')
        self.assertEqual(plan.tags,('green hair',))
        self.assertEqual(len(plan.omitted),3)

    def test_vision_reference_outfit_is_retained_without_clothing_edit(self):
        plan = filter_reference_tags(['white dress','green hair'], '微笑', 'smile')
        self.assertIn('white dress',plan.tags)

    def test_original_outfit_request_does_not_mutate_saved_profile(self):
        service = self.service()
        self.assertIn('white dress',resolve_selected_lora_triggers(service,'illustrious',[{'name':'role.safetensors'}],prompt='换回原服'))
        self.assertEqual(service.profiles_for('illustrious')['祀'].trigger_words,WORDS)

    def test_multi_character_pack_still_routes_the_requested_role(self):
        tags = self.resolve('alpha, wearing a swimsuit', 'alpha, swimsuit', words=['alpha','beta','white dress'],source_title='multi character pack')
        self.assertIn('alpha',tags)
        self.assertNotIn('beta',tags)
        self.assertNotIn('white dress',tags)

    def test_explicit_single_character_alias_matches_as_a_separate_token(self):
        service = self.service(aliases=['祀'],aliases_authoritative=True)
        self.assertEqual(len(service.match('祀，换成泳装',include_fuzzy=False).matches),1)

    def test_single_character_alias_never_matches_inside_an_ordinary_word(self):
        service = self.service(aliases=['祀'],aliases_authoritative=True)
        for prompt in ('祭祀在海边','祭祀，换衣服','abc祀','祀堂'):
            self.assertEqual(service.match(prompt,include_fuzzy=False).matches,[],prompt)

    def test_one_character_training_word_is_not_a_character_alias(self):
        service = LoraService({'illustrious':{'Alpha':{'character':'Alpha','aliases_authoritative':True,'trigger_words':['祀'],'lora':'a.safetensors'}}})
        self.assertEqual(service.match('祀，换成泳装',include_fuzzy=False).matches,[])

    def test_ambiguous_single_character_alias_is_not_silently_selected(self):
        service = self.service(aliases=['祀'],aliases_authoritative=True)
        service.load_profiles({'illustrious':{'Other':{'character':'Other','aliases':['祀'],'aliases_authoritative':True,'lora':'other.safetensors'}}})
        self.assertEqual(service.match('祀，换成泳装',include_fuzzy=False).matches,[])
