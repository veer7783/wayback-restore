<?php
/**
 * Archived Project Hindu Kush site chrome.
 * Evidence: ListingPro header-menu-dropdown and footer-style2 captures.
 */

add_action('wp_enqueue_scripts', function () {
    wp_enqueue_style('listingpr-parent-style', get_template_directory_uri() . '/style.css');
    wp_enqueue_style(
        'pkh-restored-site-chrome',
        get_stylesheet_directory_uri() . '/pkh-restored-site-chrome.css',
        array('listingpr-parent-style'),
        '1.0.0'
    );
});

add_filter('body_class', function ($classes) {
    $classes[] = 'pkh-restored-chrome';
    return $classes;
});

function pkh_restored_header() {
    $logo = esc_url(get_option('pkh_restored_header_logo_url', ''));
    ?>
    <div class="pkh-restored-header" data-pkh-global-header="restored">
        <div class="pkh-promotion"><a href="https://bit.ly/hindukushfbg">JOIN THE CONVERSATION</a></div>
        <div class="pkh-header-bar">
            <a class="pkh-header-logo" href="<?php echo esc_url(home_url('/')); ?>">
                <?php if ($logo) : ?>
                    <img src="<?php echo $logo; ?>" alt="Project Hindu Kush">
                <?php else : ?>
                    <span>PROJECT HINDUKUSH</span>
                <?php endif; ?>
            </a>
            <button class="pkh-menu-toggle" type="button" aria-expanded="false" aria-controls="pkh-global-nav">Menu</button>
            <nav id="pkh-global-nav" aria-label="Primary">
                <a href="<?php echo esc_url(home_url('/about-us/')); ?>">WHY THIS</a>
                <a href="<?php echo esc_url(home_url('/resistance/')); ?>">#RESIST</a>
                <a href="<?php echo esc_url(add_query_arg(array('select'=>'', 'lp_s_tag'=>'', 'lp_s_cat'=>'', 's'=>'home', 'post_type'=>'listing'), home_url('/'))); ?>">CRIME LIST</a>
                <a href="<?php echo esc_url(home_url('/job/')); ?>">HIRING</a>
                <a href="<?php echo esc_url(home_url('/report-hunduphobia/')); ?>">REPORT A CRIME</a>
                <a href="<?php echo esc_url(home_url('/tracker/')); ?>">STATS</a>
            </nav>
            <form class="pkh-header-search" action="<?php echo esc_url(home_url('/')); ?>" method="get">
                <label class="screen-reader-text" for="pkh-search">Search incidents</label>
                <input id="pkh-search" type="search" name="s" placeholder="Search">
                <input type="hidden" name="post_type" value="listing">
                <button type="submit">Search</button>
            </form>
        </div>
    </div>
    <?php
}
add_action('wp_body_open', 'pkh_restored_header', 20);

function pkh_restored_footer() {
    ?>
    <div class="pkh-restored-footer footer-style2" data-pkh-global-footer="restored">
        <div class="container">
            <div class="row">
                <div class="clearfix col-md-3 col-1"></div>
                <div class="clearfix col-md-3 col-2"></div>
                <div class="clearfix col-md-3 col-3"></div>
                <div class="clearfix col-md-3 col-4"></div>
            </div>
        </div>
    </div>
    <?php
}
add_action('get_footer', 'pkh_restored_footer', 20);

add_action('wp_footer', function () {
    ?>
    <script>
    (function () {
        var button = document.querySelector('.pkh-menu-toggle');
        var nav = document.getElementById('pkh-global-nav');
        if (!button || !nav) return;
        button.addEventListener('click', function () {
            var open = button.getAttribute('aria-expanded') === 'true';
            button.setAttribute('aria-expanded', open ? 'false' : 'true');
            nav.classList.toggle('is-open', !open);
        });
    }());
    </script>
    <?php
}, 100);
